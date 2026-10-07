"""
Reversing a repayment that was posted although no money arrived.

Seen in testing: an officer pressed Confirm on an online repayment the member
never paid, and the loan read "Repaid". The gate in check_loan_payment stops new
cases; these pin the clean-up of existing ones — asking Paystack about a
*confirmed* repayment and, if it has no such payment, undoing it everywhere it
took effect so the member can repay.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from communications.models import Notification
from core.context import use_tenant
from ledger.models import Account
from loans.models import Loan, LoanProduct, LoanRepayment, RepaymentInstalment
from loans.services import (LoanError, approve_loan, disburse_loan,
                            initiate_loan_repayment, record_repayment,
                            reverse_repayment)
from payments.providers import PaymentInitError
from payments.services import recheck_settled_repayment
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


@pytest.fixture
def loan(coop, member):
    with use_tenant(coop):
        Account.all_objects.get_or_create(
            cooperative=coop, code="1000",
            defaults={"name": "Cash", "kind": Account.Kind.ASSET,
                      "system": True})
        product = LoanProduct.objects.create(
            name="Emergency", interest_rate=Decimal("5"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("1000"),
            interest_rate=Decimal("5"), term_months=1)
        approve_loan(loan, approve=True)
        disburse_loan(loan)
    loan.refresh_from_db()
    return loan


def _paystack(monkeypatch, detail=None, raises=None):
    from payments import services

    class _Stub:
        def fetch_transaction(self, reference):
            if raises:
                raise raises
            return detail

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())


def _confirmed_online(loan, amount="1050.00", reference="LRPY-FAKE"):
    """The bug's aftermath: an online repayment posted with nothing paid."""
    from loans.services import confirm_loan_repayment

    with use_tenant(loan.cooperative):
        pending = initiate_loan_repayment(loan, amount=amount,
                                          reference=reference)
        return confirm_loan_repayment(pending)


def test_a_repayment_paystack_never_received_is_reversed(loan, monkeypatch):
    repayment = _confirmed_online(loan)
    loan.refresh_from_db()
    assert loan.status == Loan.Status.REPAID, "the state the member saw"

    _paystack(monkeypatch, detail=None)
    repayment, outcome = recheck_settled_repayment(repayment)

    assert outcome == "reversed"
    assert repayment.status == LoanRepayment.Status.REVERSED
    assert repayment.reversal_journal_id is not None
    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED
    assert loan.outstanding == Decimal("1050.00")
    assert not RepaymentInstalment.all_objects.filter(
        loan=loan, status=RepaymentInstalment.Status.PAID).exists()
    assert trial_balance(loan.cooperative)["balanced"] is True


def test_an_abandoned_checkout_that_was_posted_is_reversed(loan, monkeypatch):
    repayment = _confirmed_online(loan)
    _paystack(monkeypatch, detail={"success": False, "status": "abandoned",
                                   "amount": Decimal("1050"),
                                   "fee": Decimal("0")})

    repayment, outcome = recheck_settled_repayment(repayment)

    assert outcome == "reversed"
    assert "abandoned" in repayment.reversal_reason


def test_a_genuine_payment_is_left_alone(loan, monkeypatch):
    repayment = _confirmed_online(loan)
    _paystack(monkeypatch, detail={"success": True, "status": "success",
                                   "amount": Decimal("1050"),
                                   "fee": Decimal("0")})

    repayment, outcome = recheck_settled_repayment(repayment)

    assert outcome == "kept"
    loan.refresh_from_db()
    assert loan.status == Loan.Status.REPAID


def test_an_outage_never_reads_as_no_record(loan, monkeypatch):
    """Otherwise a Paystack outage would reverse every genuine repayment."""
    repayment = _confirmed_online(loan)
    _paystack(monkeypatch, raises=PaymentInitError("Paystack verify failed"))

    with pytest.raises(PaymentInitError):
        recheck_settled_repayment(repayment)

    repayment.refresh_from_db()
    assert repayment.status == LoanRepayment.Status.CONFIRMED


def test_the_member_can_repay_again_and_is_told(loan, member, monkeypatch):
    repayment = _confirmed_online(loan)
    _paystack(monkeypatch, detail=None)
    recheck_settled_repayment(repayment)
    loan.refresh_from_db()

    with use_tenant(loan.cooperative):
        again = initiate_loan_repayment(loan, amount="1050",
                                        reference="LRPY-REAL")
        assert Notification.all_objects.filter(
            membership=member, title="Repayment reversed").exists()
    assert again.status == LoanRepayment.Status.PENDING


def test_other_repayments_keep_their_place_on_the_schedule(coop, member):
    """Reversing one repayment must not undo the ones that were real."""
    with use_tenant(coop):
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=4)
        approve_loan(loan, approve=True)
        disburse_loan(loan)
        real = record_repayment(loan, amount="27500")
        fake = record_repayment(loan, amount="27500")

        reverse_repayment(fake, reason="No payment was received.")

        rows = list(RepaymentInstalment.all_objects.filter(loan=loan)
                    .order_by("sequence"))
    loan.refresh_from_db()
    assert rows[0].status == RepaymentInstalment.Status.PAID
    assert rows[1].status == RepaymentInstalment.Status.PENDING
    assert rows[1].amount_paid == Decimal("0")
    assert loan.outstanding == Decimal("82500.00")
    real.refresh_from_db()
    assert real.status == LoanRepayment.Status.CONFIRMED


def test_a_repayment_cannot_be_reversed_twice(loan):
    repayment = _confirmed_online(loan)
    reverse_repayment(repayment, reason="No payment was received.")

    with pytest.raises(LoanError):
        reverse_repayment(repayment, reason="again")


# ── From the admin and the command ─────────────────────────────────────────
def _operator():
    from accounts.models import User

    return User.objects.create_user(
        email="ops@startupripple.co", full_name="Ops", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=True)


def _request(user):
    from django.test import RequestFactory

    request = RequestFactory().post("/admin/")
    request.user = user
    collected = []
    request._messages = type("_S", (), {
        "add": lambda self, level, msg, extra="": collected.append(msg)})()
    request.collected = collected
    return request


def test_the_admin_recheck_reverses_an_unpaid_confirmed_repayment(
        loan, monkeypatch):
    from django.contrib.admin.sites import AdminSite

    from loans.admin import LoanRepaymentAdmin
    from payments import services

    repayment = _confirmed_online(loan)
    _paystack(monkeypatch, detail=None)
    monkeypatch.setattr(services, "provider_is_simulated", lambda *a: False)
    request = _request(_operator())

    LoanRepaymentAdmin(LoanRepayment, AdminSite()).recheck_selected(
        request, LoanRepayment.all_objects.filter(pk=repayment.pk))

    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED
    assert any("confirmed → reversed" in m for m in request.collected)


def test_the_admin_can_reverse_a_cash_entry(loan):
    from django.contrib.admin.sites import AdminSite

    from loans.admin import LoanRepaymentAdmin

    with use_tenant(loan.cooperative):
        cash = record_repayment(loan, amount="1050")

    LoanRepaymentAdmin(LoanRepayment, AdminSite()).reverse_selected(
        _request(_operator()), LoanRepayment.all_objects.filter(pk=cash.pk))

    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED


def test_the_command_audits_only_when_asked(loan, monkeypatch, settings):
    from django.core.management import call_command

    settings.PAYSTACK_SECRET_KEY = "sk_live_real"
    repayment = _confirmed_online(loan)
    _paystack(monkeypatch, detail=None)

    call_command("recheck_pending_payments")
    repayment.refresh_from_db()
    assert repayment.status == LoanRepayment.Status.CONFIRMED

    call_command("recheck_pending_payments", "--audit-confirmed", "--dry-run")
    repayment.refresh_from_db()
    assert repayment.status == LoanRepayment.Status.CONFIRMED

    call_command("recheck_pending_payments", "--audit-confirmed")
    repayment.refresh_from_db()
    assert repayment.status == LoanRepayment.Status.REVERSED
