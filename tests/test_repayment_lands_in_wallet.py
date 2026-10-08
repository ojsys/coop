"""
A repayment is booked where its money actually arrived.

Seen in production: a loan was repaid online, but the money never showed in the
disbursement wallet, so it could not be lent again. Every repayment debited
Cash. An online repayment lands in the platform's Paystack balance — which is
what the wallet represents — less Paystack's fee.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.core.management import call_command

from core.context import use_tenant
from ledger.models import Account
from ledger.services import Line, account_balance, post_journal
from loans.models import Loan, LoanProduct, LoanRepayment
from loans.services import (approve_loan, confirm_loan_repayment,
                            disburse_loan, initiate_loan_repayment,
                            record_repayment, report_transfer)
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _bal(coop, code):
    account = Account.all_objects.filter(cooperative=coop, code=code).first()
    return account_balance(account) if account else Decimal("0.00")


@pytest.fixture
def loan(coop, member):
    with use_tenant(coop):
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("100000"),
                                   interest_rate=Decimal("10"), term_months=4)
        approve_loan(loan, approve=True, auto_disburse=False)
        disburse_loan(loan)
    loan.refresh_from_db()
    return loan


def _online(loan, ref="LRPY-1", amount="27500"):
    with use_tenant(loan.cooperative):
        return initiate_loan_repayment(loan, amount=amount, reference=ref)


def test_an_online_repayment_reaches_the_wallet_net_of_the_fee(loan):
    coop = loan.cooperative
    with use_tenant(coop):
        confirm_loan_repayment(_online(loan),
                               receipt={"fee": Decimal("512.50"),
                                        "subaccount": False})
    loan.refresh_from_db()

    assert _bal(coop, "1020") == Decimal("26987.50")
    assert _bal(coop, "5000") == Decimal("512.50")
    # The member is credited everything they paid.
    assert loan.outstanding == Decimal("82500.00")
    assert trial_balance(coop)["balanced"] is True


def test_a_charge_split_to_the_societys_subaccount_goes_to_its_bank(loan):
    coop = loan.cooperative
    before = _bal(coop, "1010")
    with use_tenant(coop):
        confirm_loan_repayment(_online(loan), receipt={"fee": Decimal("0"),
                                                       "subaccount": True})

    assert _bal(coop, "1010") - before == Decimal("27500.00")
    assert _bal(coop, "1020") == Decimal("0.00")


def test_a_reported_bank_transfer_goes_to_the_bank(loan):
    coop = loan.cooperative
    before = _bal(coop, "1010")
    with use_tenant(coop):
        confirm_loan_repayment(report_transfer(loan, amount="27500",
                                               reference="TELLER-1"))

    assert _bal(coop, "1010") - before == Decimal("27500.00")


def test_cash_is_still_cash(loan):
    coop = loan.cooperative
    before = _bal(coop, "1000")
    with use_tenant(coop):
        record_repayment(loan, amount="27500")

    assert _bal(coop, "1000") - before == Decimal("27500.00")


def test_the_webhook_books_the_fee_paystack_reports(loan, settings):
    import hashlib
    import hmac

    from payments.services import ingest_webhook

    coop = loan.cooperative
    _online(loan, ref="LRPY-HOOK")
    body = json.dumps({"event": "charge.success", "data": {
        "id": "h1", "reference": "LRPY-HOOK", "amount": 2750000,
        "currency": "NGN", "status": "success", "fees": 51250}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body,
                   hashlib.sha512).hexdigest()

    ingest_webhook(provider_name="paystack", raw_body=body,
                   headers={"x-paystack-signature": sig})

    assert _bal(coop, "1020") == Decimal("26987.50")


def test_old_cash_bookings_are_moved_to_the_wallet(loan, monkeypatch,
                                                   settings):
    """The fix for repayments confirmed before this change."""
    from payments import services

    coop = loan.cooperative
    settings.PAYSTACK_SECRET_KEY = "sk_live_real"

    class _Stub:
        def fetch_transaction(self, reference):
            return {"success": True, "status": "success",
                    "amount": Decimal("27500"), "fee": Decimal("512.50"),
                    "subaccount": False}

    monkeypatch.setattr("loans.management.commands.move_repayments_to_wallet"
                        ".get_provider", lambda name: _Stub())
    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())
    with use_tenant(coop):
        # How a repayment was booked before: straight to Cash.
        cash = Account.all_objects.get(cooperative=coop, code="1000")
        receivable = Account.all_objects.get(cooperative=coop, code="1200")
        journal = post_journal(
            cooperative=coop, reference="OLD-RPY", memo="old repayment",
            lines=[Line(account=cash, debit=Decimal("27500")),
                   Line(account=receivable, credit=Decimal("27500"),
                        membership=loan.membership)])
        LoanRepayment.objects.create(
            loan=loan, amount=Decimal("27500"), channel="psp",
            status=LoanRepayment.Status.CONFIRMED, psp_reference="LRPY-OLD",
            journal=journal)
    cash_before = _bal(coop, "1000")

    call_command("move_repayments_to_wallet", "--dry-run")
    assert _bal(coop, "1020") == Decimal("0.00")

    call_command("move_repayments_to_wallet")
    call_command("move_repayments_to_wallet")   # idempotent

    assert _bal(coop, "1020") == Decimal("26987.50")
    assert _bal(coop, "5000") == Decimal("512.50")
    assert cash_before - _bal(coop, "1000") == Decimal("27500.00")
    assert trial_balance(coop)["balanced"] is True
