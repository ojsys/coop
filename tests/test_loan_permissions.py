"""
The officer-side loan endpoints must refuse ordinary members.

``/loans/``, ``/loan-products/`` and ``/loan-repayments/`` were all
``IsAuthenticated``, which meant any member of a cooperative could:

* **approve their own pending loan and then disburse it** — posting a real
  journal that moves the principal out of Cash. The /approvals/ queue
  advertises dual control ("whoever submits a request cannot approve it"), but
  these direct endpoints were an unguarded parallel path straight past it;
* **confirm a reported repayment**, posting to the ledger;
* **rewrite loan products** — the interest rate, maximum amount and term that
  price credit for the whole society;
* **read every other member's loan**, which ``LoanSerializer`` renders complete
  with their bank account number, phone, email and share capital.

The last one is why reads are closed too, not just the write actions: the
queryset is scoped to the cooperative, not to the caller.

These tests exist so the hole cannot quietly reopen — a permission regression is
invisible until someone exploits it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Journal
from loans.models import Loan, LoanProduct

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def officer(coop):
    """A secretary — privileged (members.manage, governance.manage)."""
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@imole.coop",
                                        full_name="Secretary", password="x")
        Membership.objects.create(
            user=user, member_no="OFF-1",
            role=Role.objects.filter(slug=Role.SECRETARY).first())
    return user


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def pending_loan(coop, member, product):
    with use_tenant(coop):
        return Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6)


@pytest.fixture
def approved_loan(coop, member, product):
    with use_tenant(coop):
        return Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)


def _headers(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


# ── A member cannot decide their own loan ───────────────────────────────────
def test_a_member_cannot_approve_their_own_loan(coop, member, pending_loan):
    resp = _client(member.user).post(
        f"/api/v1/loans/{pending_loan.id}/approve/", **_headers(coop))

    assert resp.status_code == 403
    pending_loan.refresh_from_db()
    assert pending_loan.status == Loan.Status.PENDING


def test_a_member_cannot_disburse_a_loan(coop, member, approved_loan):
    """The one that moved money: disbursement posts the principal out of Cash."""
    resp = _client(member.user).post(
        f"/api/v1/loans/{approved_loan.id}/disburse/", **_headers(coop))

    assert resp.status_code == 403
    approved_loan.refresh_from_db()
    assert approved_loan.status == Loan.Status.APPROVED
    assert not Journal.all_objects.filter(
        reference=f"LOAN-{approved_loan.id}-DISB").exists(), (
        "no disbursement journal may be posted by an unprivileged caller")


def test_a_member_cannot_record_a_repayment(coop, member, approved_loan):
    resp = _client(member.user).post(
        f"/api/v1/loans/{approved_loan.id}/repay/", {"amount": "1000"},
        format="json", **_headers(coop))

    assert resp.status_code == 403


# ── A member cannot read the loan book ──────────────────────────────────────
def test_a_member_cannot_list_every_members_loans(coop, member, pending_loan):
    """LoanSerializer carries member_bank_account_no, phone and email."""
    resp = _client(member.user).get("/api/v1/loans/", **_headers(coop))

    assert resp.status_code == 403


def test_a_member_cannot_read_a_single_loan_either(coop, member, pending_loan):
    resp = _client(member.user).get(
        f"/api/v1/loans/{pending_loan.id}/", **_headers(coop))

    assert resp.status_code == 403


def test_a_member_cannot_see_repayment_claims(coop, member):
    resp = _client(member.user).get(
        "/api/v1/loan-repayments/?status=pending", **_headers(coop))

    assert resp.status_code == 403


# ── A member cannot reprice credit ──────────────────────────────────────────
def test_a_member_cannot_create_a_loan_product(coop, member):
    resp = _client(member.user).post(
        "/api/v1/loan-products/",
        {"name": "Free money", "interest_rate": "0", "max_amount": "9999999"},
        format="json", **_headers(coop))

    assert resp.status_code == 403


def test_a_member_cannot_change_an_interest_rate(coop, member, product):
    resp = _client(member.user).patch(
        f"/api/v1/loan-products/{product.id}/", {"interest_rate": "0"},
        format="json", **_headers(coop))

    assert resp.status_code == 403
    product.refresh_from_db()
    assert product.interest_rate == Decimal("10")


def test_a_member_may_still_read_the_product_catalogue(coop, member, product):
    """Deliberately open: the catalogue is not member data, and the member app
    needs it to apply. Only writing it is privileged."""
    resp = _client(member.user).get("/api/v1/loan-products/", **_headers(coop))

    assert resp.status_code == 200


# ── The real workflow still works ───────────────────────────────────────────
def test_an_officer_can_still_approve_and_disburse(coop, officer, pending_loan):
    """The fix must not break the flow it protects."""
    client = _client(officer)

    approved = client.post(f"/api/v1/loans/{pending_loan.id}/approve/",
                           **_headers(coop))
    assert approved.status_code == 200, approved.content
    assert approved.json()["status"] == "approved"

    disbursed = client.post(f"/api/v1/loans/{pending_loan.id}/disburse/",
                            **_headers(coop))
    assert disbursed.status_code == 200, disbursed.content
    assert disbursed.json()["status"] == "disbursed"
    assert Journal.all_objects.filter(
        reference=f"LOAN-{pending_loan.id}-DISB").exists()


def test_an_officer_can_still_read_the_loan_book(coop, officer, pending_loan):
    resp = _client(officer).get("/api/v1/loans/", **_headers(coop))

    assert resp.status_code == 200


# ── The member's own path is untouched ──────────────────────────────────────
def test_a_member_can_still_see_their_own_loans(coop, member, pending_loan):
    """Closing /loans/ must not close /me/loans/ — that is how a member reads
    their own borrowing, and the only path they should have."""
    resp = _client(member.user).get("/api/v1/me/loans/", **_headers(coop))

    assert resp.status_code == 200
    rows = resp.json()
    results = rows["results"] if isinstance(rows, dict) else rows
    assert [r["id"] for r in results] == [pending_loan.id]


def test_a_member_can_still_apply(coop, member, product):
    resp = _client(member.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "50000", "term_months": 6},
        format="json", **_headers(coop))

    assert resp.status_code == 201, resp.content
    assert resp.json()["status"] == "pending"
