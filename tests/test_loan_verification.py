"""
"Was this loan actually disbursed?" — answered from evidence.

The question an officer is really asking when a member says the money never
arrived. A status field cannot answer it: it says what somebody recorded, not
what the books and the bank can show.

The case that drove this: a loan paid in **cash** leaves a journal and no payout
at all, so the Payouts page is blind to it. An officer who checked there and
found nothing would conclude the loan was never paid — when in fact the ledger
records it.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Account
from ledger.services import Line, post_journal, reverse_journal
from loans.models import Loan, LoanProduct
from loans.services import disburse_loan
from loans.verification import (CONFIRMED_CASH, CONFIRMED_TRANSFER, FAILED,
                                IN_FLIGHT, NO_JOURNAL, NOT_DISBURSED, REVERSED,
                                disbursement_evidence, recheck_disbursement)
from payments.models import Payout

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def _holder(coop, slug, email):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email,
                                       password="x")
        Membership.objects.create(
            user=user, member_no=email[:8],
            role=Role.all_objects.get(cooperative=coop, slug=slug))
    return user


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Emergency", interest_rate=Decimal("5"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def cash_loan(coop, member, product):
    """Exactly the shape of the live loan 1: paid in cash, no payout anywhere."""
    with use_tenant(coop):
        Account.all_objects.get_or_create(
            cooperative=coop, code="1000",
            defaults={"name": "Cash", "kind": Account.Kind.ASSET,
                      "system": True})
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("1000.00"),
            interest_rate=Decimal("5"), term_months=1,
            status=Loan.Status.APPROVED)
        disburse_loan(loan)
    loan.refresh_from_db()
    return loan


def _transfer_loan(coop, member, product, status, **payout_kw):
    with use_tenant(coop):
        Account.all_objects.get_or_create(
            cooperative=coop, code="1000",
            defaults={"name": "Cash", "kind": Account.Kind.ASSET,
                      "system": True})
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("1000.00"),
            interest_rate=Decimal("5"), term_months=1,
            status=Loan.Status.APPROVED)
        disburse_loan(loan)
        Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=loan.pk,
            amount=Decimal("1000.00"), status=status, reference="PO-abc",
            destination_bank_name="Access Bank",
            destination_account_no="0123456789", **payout_kw)
    loan.refresh_from_db()
    return loan


# ── Cash: the case the Payouts page cannot see ──────────────────────────────
def test_a_cash_disbursement_is_confirmed_by_its_journal(cash_loan):
    with use_tenant(cash_loan.cooperative):
        evidence = disbursement_evidence(cash_loan)

    assert evidence["verdict"] == CONFIRMED_CASH
    assert evidence["method"] == "cash"
    assert evidence["journal"]["reference"] == f"LOAN-{cash_loan.pk}-DISB"
    assert evidence["payout"] is None


def test_the_journal_amount_comes_from_the_ledger_not_the_loan(cash_loan):
    """If the two ever disagree, the officer must see the ledger's number — not
    have it quietly replaced by the figure that was expected."""
    with use_tenant(cash_loan.cooperative):
        evidence = disbursement_evidence(cash_loan)

    assert evidence["journal"]["amount"] == "1000.00"


def test_a_cash_loan_says_why_it_cannot_be_rechecked(cash_loan):
    """A disabled button with no explanation reads as a bug."""
    with use_tenant(cash_loan.cooperative):
        evidence = disbursement_evidence(cash_loan)

    assert evidence["can_recheck"] is False
    assert "cash disbursement" in evidence["recheck_unavailable_because"]
    assert "no bank transfer" in evidence["recheck_unavailable_because"]


# ── Transfers ───────────────────────────────────────────────────────────────
def test_a_settled_transfer_is_confirmed_by_the_bank(coop, member, product):
    loan = _transfer_loan(coop, member, product, Payout.Status.SUCCESS,
                          settled_at=timezone.now())

    with use_tenant(coop):
        evidence = disbursement_evidence(loan)

    assert evidence["verdict"] == CONFIRMED_TRANSFER
    assert evidence["method"] == "transfer"
    assert evidence["can_recheck"] is False, "the provider has answered"


def test_an_unsettled_transfer_is_unconfirmed_and_recheckable(coop, member,
                                                              product):
    loan = _transfer_loan(coop, member, product, Payout.Status.PENDING,
                          sent_at=timezone.now())

    with use_tenant(coop):
        evidence = disbursement_evidence(loan)

    assert evidence["verdict"] == IN_FLIGHT
    assert evidence["can_recheck"] is True
    assert "normal for a short while" in evidence["detail"]


def test_a_failed_transfer_is_reported_as_a_contradiction(coop, member,
                                                          product):
    loan = _transfer_loan(coop, member, product, Payout.Status.FAILED,
                          failure_reason="Account name mismatch")

    with use_tenant(coop):
        evidence = disbursement_evidence(loan)

    assert evidence["verdict"] == FAILED
    assert "Account name mismatch" in evidence["detail"]
    # Not recheckable, and the copy must not pretend otherwise: the provider has
    # already answered. The correction is a reversal, which the health check owns.
    assert evidence["can_recheck"] is False
    assert "already given its answer" in evidence["detail"]
    assert "final answer" in evidence["recheck_unavailable_because"]


# ── The states that should not happen ───────────────────────────────────────
def test_disbursed_with_no_journal_cannot_be_confirmed(coop, member, product):
    """Says "cannot be confirmed", not "not disbursed" — the difference between
    no evidence and evidence of absence."""
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("1000"),
            interest_rate=Decimal("5"), term_months=1,
            status=Loan.Status.DISBURSED, disbursed_at=timezone.now())

        evidence = disbursement_evidence(loan)

    assert evidence["verdict"] == NO_JOURNAL
    assert "no journal recording" in evidence["detail"]


def test_a_reversed_journal_shows_the_money_came_back(cash_loan):
    with use_tenant(cash_loan.cooperative):
        reverse_journal(cash_loan.disbursement_journal, memo="correction")
        evidence = disbursement_evidence(cash_loan)

    assert evidence["verdict"] == REVERSED
    assert evidence["journal"]["is_reversed"] is True


def test_an_undisbursed_loan_says_so_plainly(coop, member, product):
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("1000"),
            interest_rate=Decimal("5"), term_months=1,
            status=Loan.Status.APPROVED)

        evidence = disbursement_evidence(loan)

    assert evidence["verdict"] == NOT_DISBURSED
    assert evidence["can_recheck"] is False


# ── Rechecking ──────────────────────────────────────────────────────────────
def test_rechecking_applies_the_providers_answer(coop, member, product,
                                                 monkeypatch):
    """A transfer that really failed must unwind the loan — the same compensation
    the webhook performs, not a second opinion about money."""
    from payments import services

    loan = _transfer_loan(coop, member, product, Payout.Status.PENDING,
                          sent_at=timezone.now() - timedelta(hours=5))

    class _Stub:
        def fetch_transfer(self, reference):
            return {"status": "failed", "raw": {"reason": "Bank rejected it"}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())

    with use_tenant(coop):
        evidence = recheck_disbursement(loan, actor=None)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED, "the failed payment was unwound"
    assert evidence["verdict"] == NOT_DISBURSED
    assert evidence["status"] == "approved"


def test_rechecking_a_cash_loan_changes_nothing(cash_loan):
    """There is no provider to ask; it must not invent one or blank the record."""
    with use_tenant(cash_loan.cooperative):
        evidence = recheck_disbursement(cash_loan, actor=None)

    cash_loan.refresh_from_db()
    assert cash_loan.status == Loan.Status.DISBURSED
    assert evidence["verdict"] == CONFIRMED_CASH


# ── Through the API ─────────────────────────────────────────────────────────
def test_an_officer_can_ask_whether_a_loan_was_disbursed(coop, cash_loan):
    officer = _holder(coop, Role.TREASURER, "tres@imole.coop")

    resp = _client(officer).get(
        f"/api/v1/loans/{cash_loan.pk}/disbursement/", **_h(coop))

    assert resp.status_code == 200, resp.content
    assert resp.json()["verdict"] == CONFIRMED_CASH


def test_a_chairperson_can_ask_too(coop, cash_loan):
    """They are often the one a member complains to, and it is a read."""
    chair = _holder(coop, Role.CHAIRPERSON, "chair@imole.coop")

    resp = _client(chair).get(
        f"/api/v1/loans/{cash_loan.pk}/disbursement/", **_h(coop))

    assert resp.status_code == 200
    assert resp.json()["headline"] == "Paid in cash and recorded."


def test_a_chairperson_cannot_trigger_a_recheck(coop, member, product):
    """It applies the answer, which can reverse a journal — that stays privileged."""
    loan = _transfer_loan(coop, member, product, Payout.Status.PENDING,
                          sent_at=timezone.now())
    chair = _holder(coop, Role.CHAIRPERSON, "chair2@imole.coop")

    resp = _client(chair).post(
        f"/api/v1/loans/{loan.pk}/recheck-disbursement/", **_h(coop))

    assert resp.status_code == 403


def test_a_member_cannot_ask_about_another_members_loan(coop, cash_loan,
                                                        member):
    resp = _client(member.user).get(
        f"/api/v1/loans/{cash_loan.pk}/disbursement/", **_h(coop))

    assert resp.status_code == 403


def test_an_officer_can_recheck_through_the_api(coop, member, product,
                                               monkeypatch):
    from payments import services

    loan = _transfer_loan(coop, member, product, Payout.Status.PENDING,
                          sent_at=timezone.now())
    officer = _holder(coop, Role.TREASURER, "tres2@imole.coop")

    class _Stub:
        def fetch_transfer(self, reference):
            return {"status": "success", "raw": {}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())

    resp = _client(officer).post(
        f"/api/v1/loans/{loan.pk}/recheck-disbursement/", **_h(coop))

    assert resp.status_code == 200, resp.content
    assert resp.json()["verdict"] == CONFIRMED_TRANSFER


def test_a_provider_outage_is_not_reported_as_not_disbursed(coop, member,
                                                            product,
                                                            monkeypatch):
    """The dangerous failure mode: an unreachable provider reading as proof that
    no money was sent."""
    from payments import services
    from payments.providers import PaymentInitError

    loan = _transfer_loan(coop, member, product, Payout.Status.PENDING,
                          sent_at=timezone.now())
    officer = _holder(coop, Role.TREASURER, "tres3@imole.coop")

    class _Down:
        def fetch_transfer(self, reference):
            raise PaymentInitError("Could not reach Paystack.")

    monkeypatch.setattr(services, "get_provider", lambda n: _Down())

    resp = _client(officer).post(
        f"/api/v1/loans/{loan.pk}/recheck-disbursement/", **_h(coop))

    assert resp.status_code == 502
    assert "Paystack" in resp.json()["detail"]
    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED, "nothing may have changed"
