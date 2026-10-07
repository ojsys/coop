"""
Settling an online loan repayment without the member's return from checkout.

A repayment used to settle only when the member came back through the
checkout's return URL. The provider's webhook was filed as "unmatched" because
reconciliation only knew about contributions — so a member who paid and closed
the tab stayed in arrears with the money already taken. These pin the webhook,
the admin recheck and the bulk command that now settle it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from django.conf import settings as django_settings
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.test import RequestFactory

from accounts.models import User
from core.context import use_tenant
from ledger.models import Account
from loans.admin import LoanRepaymentAdmin
from loans.models import Loan, LoanProduct, LoanRepayment
from loans.services import disburse_loan, initiate_loan_repayment
from payments.models import PaymentEvent
from payments.services import ingest_webhook

pytestmark = pytest.mark.django_db


@pytest.fixture
def loan(coop, member):
    with use_tenant(coop):
        Account.all_objects.get_or_create(
            cooperative=coop, code="1000",
            defaults={"name": "Cash", "kind": Account.Kind.ASSET,
                      "system": True})
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)
        disburse_loan(loan)
    loan.refresh_from_db()
    return loan


def _pending(loan, reference="LNR-1", amount="10000"):
    with use_tenant(loan.cooperative):
        return initiate_loan_repayment(loan, amount=amount, reference=reference)


def _webhook(reference, amount_kobo, event_id="evt-1"):
    body = json.dumps({"event": "charge.success", "data": {
        "id": event_id, "reference": reference, "amount": amount_kobo,
        "currency": "NGN", "status": "success"}}).encode()
    sig = hmac.new(django_settings.PAYSTACK_SECRET_KEY.encode(), body,
                   hashlib.sha512).hexdigest()
    return ingest_webhook(provider_name="paystack", raw_body=body,
                          headers={"x-paystack-signature": sig})


def _settled(loan):
    return LoanRepayment.all_objects.filter(
        loan=loan, status=LoanRepayment.Status.CONFIRMED)


# ── The webhook ─────────────────────────────────────────────────────────────
def test_the_webhook_settles_a_pending_repayment(loan):
    _pending(loan)

    event = _webhook("LNR-1", 1_000_000)

    assert event.status == PaymentEvent.Status.MATCHED
    assert _settled(loan).count() == 1
    assert _settled(loan).get().journal_id is not None
    assert not LoanRepayment.all_objects.filter(
        loan=loan, status=LoanRepayment.Status.PENDING).exists()


def test_a_replayed_settlement_does_not_post_twice(loan):
    """The member's return and the webhook both arrive — once only."""
    from payments.services import verify_loan_payment

    _pending(loan)
    verify_loan_payment(loan.cooperative, "LNR-1")   # the member came back

    event = _webhook("LNR-1", 1_000_000, event_id="evt-late")

    assert event.status == PaymentEvent.Status.DUPLICATE
    assert _settled(loan).count() == 1


def test_a_different_amount_is_left_for_an_operator(loan):
    _pending(loan)

    event = _webhook("LNR-1", 500_000)

    assert event.status == PaymentEvent.Status.PARTIAL
    assert _settled(loan).count() == 0


def test_a_reported_bank_transfer_is_never_settled_by_a_webhook(loan):
    """Its reference is the member's teller number, not one we issued."""
    from loans.services import report_transfer

    with use_tenant(loan.cooperative):
        report_transfer(loan, amount="10000", reference="TELLER-9")

    event = _webhook("TELLER-9", 1_000_000)

    assert event.status != PaymentEvent.Status.MATCHED
    assert _settled(loan).count() == 0


# ── The admin action and the command ───────────────────────────────────────
def _request(user):
    request = RequestFactory().post("/admin/")
    request.user = user
    collected = []
    request._messages = type(
        "_S", (), {"add": lambda self, level, msg, extra="": collected.append(
            msg)})()
    request.collected = collected
    return request


def _paid(monkeypatch, paid=True):
    from payments import services

    class _Stub:
        def verify_transaction(self, reference):
            return paid

        def fetch_transaction(self, reference):
            return {"success": paid, "status": "success" if paid else
                    "abandoned", "amount": None, "fee": Decimal("0")}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())
    monkeypatch.setattr(services, "provider_is_simulated", lambda *a: False)


def test_an_operator_can_settle_a_paid_but_pending_repayment(loan,
                                                            monkeypatch):
    _paid(monkeypatch)
    repayment = _pending(loan)
    operator = User.objects.create_user(
        email="ops@startupripple.co", full_name="Ops", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=True)
    request = _request(operator)

    LoanRepaymentAdmin(LoanRepayment, AdminSite()).recheck_selected(
        request, LoanRepayment.all_objects.filter(pk=repayment.pk))

    assert _settled(loan).count() == 1
    assert any("pending → confirmed" in m for m in request.collected)


def test_an_unpaid_repayment_stays_pending(loan, monkeypatch, settings):
    settings.PAYSTACK_SECRET_KEY = "sk_live_real"
    _paid(monkeypatch, paid=False)
    _pending(loan)

    call_command("recheck_pending_payments")

    assert _settled(loan).count() == 0


def test_the_command_settles_stuck_repayments(loan, monkeypatch, settings):
    settings.PAYSTACK_SECRET_KEY = "sk_live_real"
    _paid(monkeypatch)
    _pending(loan)

    call_command("recheck_pending_payments")

    assert _settled(loan).count() == 1
