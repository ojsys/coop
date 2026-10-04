"""
Approving a loan pays it out.

This is the whole point of the payout rail: an officer approves, and the money
leaves for the member's bank immediately, drawn from the society's disbursement
wallet.

It only happens where it actually can. The loan needs a bank code and account
number on its *snapshot* — the destination frozen when it was applied for, so a
later edit cannot redirect it — and the wallet must cover the principal. When
either is missing the loan stays APPROVED for the cash path and the officer is
told which it was, because an approved loan nobody noticed could not be paid is
how a member ends up waiting in silence.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Account, LedgerEntry
from ledger.services import Line, post_journal
from loans.models import Loan, LoanProduct, RepaymentInstalment
from loans.services import approve_loan
from payments.models import Payout
from payments.services import wallet_balance

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def _officer(coop, email="sec@imole.coop", slug=Role.SECRETARY):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name=email, password="x")
        Membership.objects.create(
            user=u, member_no=email[:6],
            role=Role.objects.filter(slug=slug).first())
    return u


def _acct(coop, code, name, kind):
    with use_tenant(coop):
        a, _ = Account.all_objects.get_or_create(
            cooperative=coop, code=code,
            defaults={"name": name, "kind": kind, "system": True})
    return a


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def banked(coop, member):
    member.bank_name = "Access Bank"
    member.bank_code = "044"
    member.bank_account_no = "0123456789"
    member.save(update_fields=["bank_name", "bank_code", "bank_account_no"])
    return member


@pytest.fixture
def funded(coop):
    wallet = _acct(coop, "1020", "Disbursement Wallet", Account.Kind.ASSET)
    settle = _acct(coop, "1010", "Bank / PSP Settlement", Account.Kind.ASSET)
    with use_tenant(coop):
        post_journal(cooperative=coop, reference="TEST-FUND", memo="fund",
                    lines=[Line(account=wallet, debit=Decimal("500000.00")),
                           Line(account=settle, credit=Decimal("500000.00"))])
    return coop


@pytest.fixture
def stub(monkeypatch):
    from payments import services

    class _Stub:
        def create_transfer_recipient(self, **kw):
            return "RCP_member"

        def initiate_transfer(self, **kw):
            _Stub.sent = kw
            return {"status": "pending", "transfer_code": "TRF_1",
                    "raw": {"status": "pending"}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())
    return _Stub


def _apply(coop, member, product):
    """Apply through the API so the destination snapshot is taken."""
    resp = _client(member.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "100000", "term_months": 6},
        format="json", **_h(coop))
    assert resp.status_code == 201, resp.content
    return Loan.all_objects.get(pk=resp.json()["id"])


# ── The happy path ──────────────────────────────────────────────────────────
def test_approving_pays_the_loan_out(funded, banked, product, stub):
    loan = _apply(funded, banked, product)
    officer = _officer(funded)

    with use_tenant(funded):
        approve_loan(loan, actor=officer, approve=True)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED
    assert loan.disbursed_at is not None
    payout = Payout.all_objects.get()
    assert payout.kind == Payout.Kind.LOAN
    assert payout.object_id == loan.id
    assert payout.amount == Decimal("100000.00")


def test_it_pays_the_snapshotted_destination(funded, banked, product, stub):
    """Not the member's current details — the account frozen at application."""
    loan = _apply(funded, banked, product)
    # The member changes their account after applying. It must be ignored.
    banked.bank_account_no = "9999999999"
    banked.save(update_fields=["bank_account_no"])

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    payout = Payout.all_objects.get()
    assert payout.destination_account_no == "0123456789"


def test_the_wallet_pays_for_it(funded, banked, product, stub):
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    assert wallet_balance(funded) == Decimal("400000.00")


def test_the_journal_debits_receivable_and_credits_the_wallet(
        funded, banked, product, stub):
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    payout = Payout.all_objects.get()
    lines = {e.account.code: e
             for e in LedgerEntry.all_objects.filter(journal=payout.journal)}
    assert lines["1200"].debit == Decimal("100000.00")
    assert lines["1020"].credit == Decimal("100000.00")


def test_the_disbursement_appears_on_the_members_statement(
        funded, banked, product, stub):
    """The statement is built from entries tagged with the membership. Without
    that tag an electronically disbursed loan would be invisible to the member
    while a cash one showed up."""
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    payout = Payout.all_objects.get()
    debit = LedgerEntry.all_objects.get(journal=payout.journal,
                                        account__code="1200")
    assert debit.membership_id == banked.pk


def test_the_repayment_schedule_is_built(funded, banked, product, stub):
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    assert RepaymentInstalment.all_objects.filter(loan=loan).count() == 6


def test_the_principal_is_booked_once_not_twice(funded, banked, product, stub):
    """send_payout writes the disbursement journal; a second one here would
    double-count the principal."""
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    debits = LedgerEntry.all_objects.filter(account__code="1200",
                                            debit=Decimal("100000.00"))
    assert debits.count() == 1


def test_the_books_stay_balanced(funded, banked, product, stub):
    from reports.financials import trial_balance

    loan = _apply(funded, banked, product)
    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)

    assert trial_balance(funded)["balanced"] is True


# ── When it cannot pay ──────────────────────────────────────────────────────
def test_an_empty_wallet_leaves_the_loan_approved(coop, banked, product, stub):
    """The loan is still good — it just has to be paid another way."""
    loan = _apply(coop, banked, product)

    with use_tenant(coop):
        approve_loan(loan, actor=_officer(coop), approve=True)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED
    assert Payout.all_objects.count() == 0


def test_an_empty_wallet_says_why(coop, banked, product, stub):
    loan = _apply(coop, banked, product)

    with use_tenant(coop):
        result = approve_loan(loan, actor=_officer(coop), approve=True)

    assert result.auto_disbursement_error
    assert "Fund the wallet" in result.auto_disbursement_error


def test_no_bank_code_leaves_it_approved_for_cash(funded, member, product,
                                                  stub):
    """Every member whose details predate bank selection is in this state."""
    loan = _apply(funded, member, product)

    with use_tenant(funded):
        result = approve_loan(loan, actor=_officer(funded), approve=True)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED
    assert "cash" in result.auto_disbursement_error.lower()
    assert Payout.all_objects.count() == 0


def test_a_provider_refusal_does_not_break_the_approval(funded, banked,
                                                        product, monkeypatch):
    """A refused transfer must not surface as a 500, and must not lose the
    approval — the decision stands, the payment is retried another way."""
    from payments import services
    from payments.providers import PaymentInitError

    class _Boom:
        def create_transfer_recipient(self, **kw):
            raise PaymentInitError("Paystack rejected the recipient.")

    monkeypatch.setattr(services, "get_provider", lambda n: _Boom())
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        result = approve_loan(loan, actor=_officer(funded), approve=True)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED
    assert "Paystack" in result.auto_disbursement_error
    assert Payout.all_objects.count() == 0
    assert wallet_balance(funded) == Decimal("500000.00")


def test_rejecting_pays_nothing(funded, banked, product, stub):
    loan = _apply(funded, banked, product)

    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=False)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.REJECTED
    assert Payout.all_objects.count() == 0


# ── Over the API ────────────────────────────────────────────────────────────
def test_the_approve_endpoint_reports_the_payout(funded, banked, product, stub):
    loan = _apply(funded, banked, product)
    officer = _officer(funded)

    resp = _client(officer).post(f"/api/v1/loans/{loan.id}/approve/",
                                 **_h(funded))

    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "disbursed"
    assert resp.json()["payout_reference"].startswith("PO-")
    assert resp.json()["auto_disbursement_error"] is None


def test_the_approve_endpoint_reports_why_it_could_not_pay(coop, banked,
                                                           product, stub):
    loan = _apply(coop, banked, product)
    officer = _officer(coop)

    resp = _client(officer).post(f"/api/v1/loans/{loan.id}/approve/",
                                 **_h(coop))

    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "approved"
    assert "Fund the wallet" in resp.json()["auto_disbursement_error"]
    assert resp.json()["payout_reference"] is None


def test_the_approvals_queue_also_pays_out(funded, banked, product, stub):
    """Approving through the maker-checker queue must behave the same way."""
    from approvals.models import ApprovalRequest
    from approvals.services import decide_request, submit_request

    loan = _apply(funded, banked, product)
    maker = _officer(funded, "maker@imole.coop")
    checker = _officer(funded, "checker@imole.coop")

    with use_tenant(funded):
        request_obj = submit_request(
            cooperative=funded, action=ApprovalRequest.Action.LOAN_APPROVE,
            object_id=loan.id, requested_by=maker)
        decide_request(request_obj, actor=checker, approve=True)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED
    assert Payout.all_objects.count() == 1
