"""
The line between "may look" and "may act".

A Chairperson holds governance.approve and reports.view — none of
``Role.PRIVILEGED_PERMISSIONS`` — so they cannot post to the ledger or manage
members. But they sit on the console to approve things, and approving without
being able to see the loan book is not a job anyone can do.

So the loan book is readable by any **office-holder** and writable only by a
privileged officer. The distinction matters because the obvious shortcut,
``IsPrivilegedOfficerOrReadOnly``, opens reads to *any authenticated member* —
and this queryset is scoped to the cooperative, not to the caller, with
LoanSerializer rendering every member's bank account number, phone and email.
That would hand the society's loan book to everyone in it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from loans.models import Loan, LoanProduct

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


@pytest.fixture
def chair(coop):
    return _holder(coop, Role.CHAIRPERSON, "chair@imole.coop")


@pytest.fixture
def plain(coop):
    return _holder(coop, Role.MEMBER, "plain@imole.coop")


@pytest.fixture
def loan(coop, member):
    with use_tenant(coop):
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        return Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


# ── The role property the whole thing rests on ──────────────────────────────
def test_a_chairperson_is_an_officer_but_not_privileged(coop):
    chair = Role.all_objects.get(cooperative=coop, slug=Role.CHAIRPERSON)

    assert chair.is_officer is True
    assert chair.is_privileged is False


def test_a_plain_member_is_neither(coop):
    role = Role.all_objects.get(cooperative=coop, slug=Role.MEMBER)

    assert role.is_officer is False
    assert role.is_privileged is False


def test_a_societys_own_role_counts_as_an_office(coop):
    """Judged by permissions, not by slug — so a custom role works without a
    code change, which a slug comparison would have missed."""
    with use_tenant(coop):
        auditor = Role.objects.create(slug="auditor", name="Auditor",
                                      permissions=["reports.view"])

    assert auditor.is_officer is True
    assert auditor.is_privileged is False


def test_a_role_with_only_member_permissions_is_not_an_office(coop):
    with use_tenant(coop):
        role = Role.objects.create(slug="voter", name="Voter",
                                   permissions=["governance.vote"])

    assert role.is_officer is False


# ── Reading ─────────────────────────────────────────────────────────────────
def test_a_chairperson_can_read_the_loan_book(coop, chair, loan):
    resp = _client(chair).get("/api/v1/loans/", **_h(coop))

    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_a_chairperson_can_see_the_awaiting_payment_queue(coop, chair, loan):
    resp = _client(chair).get("/api/v1/loans/health/", **_h(coop))

    assert resp.status_code == 200
    assert resp.json()["awaiting_payment"] == 1


def test_a_plain_member_still_cannot_read_the_loan_book(coop, plain, loan):
    """The leak this gating closed: every member's bank details in one list."""
    resp = _client(plain).get("/api/v1/loans/", **_h(coop))

    assert resp.status_code == 403


def test_a_plain_member_still_cannot_read_the_queue(coop, plain, loan):
    resp = _client(plain).get("/api/v1/loans/health/", **_h(coop))

    assert resp.status_code == 403


def test_an_outsider_cannot_read_another_societys_loan_book(other_coop, chair,
                                                           loan):
    """Holding an office in one cooperative grants nothing in another."""
    resp = _client(chair).get("/api/v1/loans/",
                             HTTP_X_COOPERATIVE_ID=str(other_coop.id))

    assert resp.status_code == 403


# ── Acting ──────────────────────────────────────────────────────────────────
def test_a_chairperson_cannot_approve_a_loan(coop, chair, member, loan):
    with use_tenant(coop):
        pending = Loan.objects.create(
            membership=member, product=loan.product,
            principal=Decimal("50000"), interest_rate=Decimal("10"),
            term_months=6, status=Loan.Status.PENDING)

    resp = _client(chair).post(f"/api/v1/loans/{pending.id}/approve/", **_h(coop))

    assert resp.status_code == 403
    pending.refresh_from_db()
    assert pending.status == Loan.Status.PENDING


def test_a_chairperson_cannot_release_a_payment(coop, chair, loan):
    resp = _client(chair).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(coop))

    assert resp.status_code == 403


def test_a_chairperson_cannot_record_a_cash_disbursement(coop, chair, loan):
    resp = _client(chair).post(f"/api/v1/loans/{loan.id}/disburse/", **_h(coop))

    assert resp.status_code == 403
    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED


def test_a_chairperson_cannot_create_a_loan(coop, chair, member, loan):
    resp = _client(chair).post("/api/v1/loans/", {
        "membership": member.id, "product": loan.product.id,
        "principal": "10000", "term_months": 6,
    }, **_h(coop))

    assert resp.status_code == 403


# ── The frontend's entry rule is the same rule ──────────────────────────────
def test_the_membership_summary_reports_both_flags(coop, chair):
    """`is_officer` is exposed so the console stops inferring office from the
    role slug, which diverges as soon as a society adds a role."""
    body = _client(chair).get("/api/v1/me/", **_h(coop)).json()
    summary = next(m for m in body["memberships"]
                   if m["cooperative_id"] == coop.id)

    assert summary["is_officer"] is True
    assert summary["is_privileged"] is False


# ── Repayment claims ────────────────────────────────────────────────────────
def _claim(coop, member, product):
    """A member-reported transfer waiting to be verified."""
    from loans.models import LoanRepayment

    with use_tenant(coop):
        disbursed = Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.DISBURSED)
        return LoanRepayment.objects.create(
            loan=disbursed, amount=Decimal("20000"),
            channel=LoanRepayment.Channel.TRANSFER,
            status=LoanRepayment.Status.PENDING,
            note="GTB transfer, 3 Oct")


def test_a_chairperson_can_see_reported_transfers(coop, chair, member, loan):
    """Knowing which claims are waiting is part of knowing the state of the
    society's lending."""
    _claim(coop, member, loan.product)

    resp = _client(chair).get("/api/v1/loan-repayments/?status=pending",
                             **_h(coop))

    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_a_chairperson_cannot_confirm_a_repayment(coop, chair, member, loan):
    """Confirming posts to the ledger, so it stays privileged."""
    claim = _claim(coop, member, loan.product)

    resp = _client(chair).post(
        f"/api/v1/loan-repayments/{claim.id}/confirm/", **_h(coop))

    assert resp.status_code == 403
    claim.refresh_from_db()
    assert claim.status == "pending"
    assert claim.journal_id is None, "nothing may have reached the ledger"


def test_a_chairperson_cannot_dismiss_a_claim(coop, chair, member, loan):
    claim = _claim(coop, member, loan.product)

    resp = _client(chair).post(
        f"/api/v1/loan-repayments/{claim.id}/reject/", **_h(coop))

    assert resp.status_code == 403
    claim.refresh_from_db()
    assert claim.status == "pending"


def test_a_plain_member_cannot_see_other_members_claims(coop, plain, member,
                                                        loan):
    """The list carries other members' bank details; members report their own
    transfers through /me/loans/{id}/report-transfer/ instead."""
    _claim(coop, member, loan.product)

    resp = _client(plain).get("/api/v1/loan-repayments/?status=pending",
                             **_h(coop))

    assert resp.status_code == 403
