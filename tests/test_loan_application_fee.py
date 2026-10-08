"""
The loan application (admin) fee.

Set per loan product as a percentage, a flat sum, or both. Captured on the loan
when it is applied for, and deducted at disbursement: the member owes and
repays the full principal but receives principal − fee. Booked in the same
journal as the disbursement, so every way of undoing a disbursement undoes the
fee too.

    debit  1200 Loans Receivable    principal
    credit 1000 Cash / 1020 Wallet  principal − fee
    credit 4110 Loan Fee Income     fee
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.context import use_tenant
from ledger.models import Account
from ledger.services import Line, account_balance, post_journal
from loans.models import Loan, LoanProduct
from loans.services import approve_loan, disburse_loan, unwind_disbursement
from payments.models import Payout
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _client(user, coop=None):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    extra = {"HTTP_X_COOPERATIVE_ID": str(coop.id)} if coop else {}
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}", **extra)
    return client


def _acct(coop, code, name="", kind=Account.Kind.ASSET):
    account, _ = Account.all_objects.get_or_create(
        cooperative=coop, code=code,
        defaults={"name": name or code, "kind": kind, "system": True})
    return account


@pytest.fixture
def product(coop):
    """2% plus ₦500: a ₦100,000 loan carries a ₦2,500 fee."""
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12,
            application_fee_percent=Decimal("2"),
            application_fee_flat=Decimal("500"))


@pytest.fixture
def applied(coop, member, product):
    member.bank_name, member.bank_code = "Access Bank", "044"
    member.bank_account_no = "0123456789"
    member.save()
    resp = _client(member.user, coop).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "100000", "term_months": 6},
        format="json")
    assert resp.status_code == 201, resp.content
    return Loan.all_objects.get(pk=resp.json()["id"])


def _approve(loan, coop, *, auto=False):
    with use_tenant(coop):
        return approve_loan(loan, approve=True, auto_disburse=auto)


# ── The fee itself ──────────────────────────────────────────────────────────
def test_the_fee_is_flat_plus_percent(product):
    assert product.application_fee_for("100000") == Decimal("2500.00")


def test_the_applicant_is_shown_the_fee_and_what_they_will_receive(
        coop, member, product, applied):
    data = _client(member.user, coop).get(
        f"/api/v1/me/loans/{applied.id}/").json()

    assert data["application_fee"] == "2500.00"
    assert data["amount_to_disburse"] == "97500.00"


def test_the_fee_is_held_at_what_the_applicant_was_shown(product, applied):
    product.application_fee_percent = Decimal("5")
    product.save()
    applied.refresh_from_db()

    assert applied.application_fee == Decimal("2500.00")


def test_a_fee_that_swallows_the_loan_is_refused(coop, member, product):
    product.application_fee_flat = Decimal("5000")
    product.save()

    resp = _client(member.user, coop).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "4000", "term_months": 2},
        format="json")

    assert resp.status_code == 400
    assert "application fee" in str(resp.json())


def test_a_loan_without_a_fee_disburses_as_before(coop, member):
    with use_tenant(coop):
        plain = LoanProduct.objects.create(
            name="Plain", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(membership=member, product=plain,
                                   principal=Decimal("1000"),
                                   interest_rate=Decimal("10"), term_months=1)
    _approve(loan, coop)
    with use_tenant(coop):
        disburse_loan(loan)

    assert account_balance(_acct(coop, "1000")) == Decimal("-1000.00")


# ── Cash disbursement ───────────────────────────────────────────────────────
def test_cash_pays_out_principal_less_fee_and_books_the_fee(coop, applied):
    _approve(applied, coop)
    applied.refresh_from_db()
    with use_tenant(coop):
        disburse_loan(applied)
    applied.refresh_from_db()

    assert account_balance(_acct(coop, "1000")) == Decimal("-97500.00")
    assert account_balance(_acct(coop, "1200")) == Decimal("100000.00")
    assert account_balance(_acct(coop, "4110")) == Decimal("2500.00")
    # The member still repays the full principal plus interest on it.
    assert applied.outstanding == Decimal("110000.00")
    assert trial_balance(coop)["balanced"] is True


def test_reversing_the_disbursement_reverses_the_fee(coop, applied):
    _approve(applied, coop)
    applied.refresh_from_db()
    with use_tenant(coop):
        disburse_loan(applied)
        unwind_disbursement(applied, reason="Paid in error")

    assert account_balance(_acct(coop, "4110")) == Decimal("0.00")
    assert account_balance(_acct(coop, "1000")) == Decimal("0.00")
    assert account_balance(_acct(coop, "1200")) == Decimal("0.00")


# ── Electronic disbursement ────────────────────────────────────────────────
class _Provider:
    def create_transfer_recipient(self, **kw):
        return "RCP_x"

    def initiate_transfer(self, **kw):
        _Provider.sent = kw["amount"]
        return {"status": "pending", "transfer_code": "TRF_x", "raw": {}}


@pytest.fixture
def wallet(coop, monkeypatch):
    from payments import services

    monkeypatch.setattr(services, "get_provider", lambda name: _Provider())
    with use_tenant(coop):
        post_journal(cooperative=coop, reference="FUND", memo="fund",
                     lines=[Line(account=_acct(coop, "1020"),
                                 debit=Decimal("500000")),
                            Line(account=_acct(coop, "1010"),
                                 credit=Decimal("500000"))])


def test_only_principal_less_fee_is_transferred(coop, applied, wallet):
    loan = _approve(applied, coop, auto=True)

    assert loan.status == Loan.Status.DISBURSED
    assert _Provider.sent == Decimal("97500.00")
    assert Payout.all_objects.get().amount == Decimal("97500.00")
    assert account_balance(_acct(coop, "1020")) == Decimal("402500.00")
    assert account_balance(_acct(coop, "4110")) == Decimal("2500.00")
    assert account_balance(_acct(coop, "1200")) == Decimal("100000.00")


def test_a_failed_transfer_gives_the_fee_back_too(coop, applied, wallet):
    from payments.services import _apply_transfer_status

    _approve(applied, coop, auto=True)
    _apply_transfer_status(Payout.all_objects.get(), "failed",
                           raw={"message": "Account closed"})
    applied.refresh_from_db()

    assert applied.status == Loan.Status.APPROVED
    assert account_balance(_acct(coop, "1020")) == Decimal("500000.00")
    assert account_balance(_acct(coop, "4110")) == Decimal("0.00")
    assert account_balance(_acct(coop, "1200")) == Decimal("0.00")
