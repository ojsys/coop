"""
Paystack's fee on contributions is the member's to pay, as on loan repayments.

Checkout charges the contribution plus the fee, so the society's account
receives the whole contribution and the member is credited all of it. Before
this, the full contribution was booked as received although Paystack's fee
never arrived — overstating the settlement account.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from contributions.models import Contribution
from contributions.services import initiate_contribution
from core.context import use_tenant
from ledger.models import Account
from ledger.services import account_balance, member_balance
from payments.fees import gross_up, paystack_fee
from payments.models import PaymentEvent
from payments.services import ingest_webhook, verify_payment
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _bal(coop, code):
    account = Account.all_objects.filter(cooperative=coop, code=code).first()
    return account_balance(account) if account else Decimal("0.00")


def _pending(coop, member, dues_type, ref="CTB-1", amount="5000"):
    with use_tenant(coop):
        return initiate_contribution(cooperative=coop, membership=member,
                                     contribution_type=dues_type,
                                     amount=amount, psp_reference=ref)


def _webhook(reference, paid, fee, event_id="evt-1"):
    body = json.dumps({"event": "charge.success", "data": {
        "id": event_id, "reference": reference,
        "amount": int(Decimal(paid) * 100), "fees": int(Decimal(fee) * 100),
        "currency": "NGN", "status": "success"}}).encode()
    from django.conf import settings
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body,
                   hashlib.sha512).hexdigest()
    return ingest_webhook(provider_name="paystack", raw_body=body,
                          headers={"x-paystack-signature": sig})


def test_the_society_receives_the_whole_contribution(coop, member, dues_type):
    _pending(coop, member, dues_type)
    paid = gross_up("5000")                                  # 5,177.67

    event = _webhook("CTB-1", paid, paystack_fee(paid))

    assert event.status == PaymentEvent.Status.MATCHED
    assert _bal(coop, "1010") == Decimal("5000.00")
    assert _bal(coop, "5000") == Decimal("0.00")
    assert member_balance(member) == Decimal("5000.00")
    assert trial_balance(coop)["balanced"] is True


def test_a_checkout_opened_before_the_change_still_settles(coop, member,
                                                           dues_type):
    """Paid at the plain amount: the fee came out of it, a real cost."""
    _pending(coop, member, dues_type)

    event = _webhook("CTB-1", "5000", "175")

    assert event.status == PaymentEvent.Status.MATCHED
    assert _bal(coop, "1010") == Decimal("4825.00")
    assert _bal(coop, "5000") == Decimal("175.00")
    assert member_balance(member) == Decimal("5000.00")


def test_any_other_amount_is_left_for_a_person(coop, member, dues_type):
    contribution = _pending(coop, member, dues_type)

    event = _webhook("CTB-1", "4000", "60")

    assert event.status == PaymentEvent.Status.PARTIAL
    contribution.refresh_from_db()
    assert contribution.status == Contribution.Status.PENDING


def test_confirming_on_return_books_the_same_way(coop, member, dues_type,
                                                 monkeypatch):
    from payments import services

    paid = gross_up("5000")

    class _Stub:
        def fetch_transaction(self, reference):
            return {"success": True, "status": "success", "amount": paid,
                    "fee": paystack_fee(paid), "subaccount": False}

    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())
    _pending(coop, member, dues_type)

    verify_payment(coop, "CTB-1")

    assert _bal(coop, "1010") == Decimal("5000.00")
    assert member_balance(member) == Decimal("5000.00")


def test_checkout_and_quote_charge_the_fee_on_top(coop, member, dues_type):
    token, _ = Token.objects.get_or_create(user=member.user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}",
                       HTTP_X_COOPERATIVE_ID=str(coop.id))

    quote = client.get("/api/v1/fee-quote/", {"amount": "5000"}).json()
    resp = client.post("/api/v1/contributions/initiate/", {
        "membership": member.id, "contribution_type": dues_type.id,
        "amount": "5000", "channel": "psp"}, format="json")

    assert quote == {"amount": "5000.00", "fee": "177.67",
                     "total": "5177.67"}
    assert resp.status_code in (200, 201), resp.content
    assert resp.json()["amount_kobo"] == 517767
    assert resp.json()["fee"] == "177.67"


# ── Regression: the repayment webhook ───────────────────────────────────────
def test_a_repayment_paid_with_the_fee_settles_from_the_webhook(coop, member):
    """Its exact-amount check flagged every fee-inclusive repayment partial."""
    from loans.models import Loan, LoanProduct, LoanRepayment
    from loans.services import approve_loan, disburse_loan, initiate_loan_repayment

    with use_tenant(coop):
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("100000"),
                                   interest_rate=Decimal("10"), term_months=4)
        approve_loan(loan, approve=True, auto_disburse=False)
        disburse_loan(loan)
        initiate_loan_repayment(loan, amount="27500", reference="LRPY-FEE")
    paid = gross_up("27500")

    event = _webhook("LRPY-FEE", paid, paystack_fee(paid), event_id="evt-r")

    assert event.status == PaymentEvent.Status.MATCHED
    assert LoanRepayment.all_objects.filter(
        loan=loan, status=LoanRepayment.Status.CONFIRMED).count() == 1
    assert _bal(coop, "1020") == Decimal("27500.00")
