"""Phase 11: maker-checker approvals with segregation of duties."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from loans.models import Loan, LoanProduct

pytestmark = pytest.mark.django_db


def _officer(coop, email):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name=email, password="x")
        Membership.objects.create(user=u, member_no=email[:6],
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


@pytest.fixture
def approved_loan(coop, member):
    with use_tenant(coop):
        product = LoanProduct.objects.create(name="P", interest_rate=Decimal("0"))
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("10000"), status=Loan.Status.APPROVED)
    return loan


def test_maker_cannot_approve_own_request(coop, approved_loan):
    maker = _officer(coop, "maker@i.co")
    # A second officer exists, so the sole-Secretary exemption does not apply.
    _officer(coop, "checker@i.co")
    client = _client(maker)
    r = client.post("/api/v1/approvals/",
                    {"action": "loan.disburse", "object_id": approved_loan.id},
                    format="json")
    assert r.status_code == 201, r.content
    aid = r.json()["id"]
    # Same officer tries to approve -> blocked (segregation of duties).
    resp = client.post(f"/api/v1/approvals/{aid}/approve/")
    assert resp.status_code == 400
    approved_loan.refresh_from_db()
    assert approved_loan.status == Loan.Status.APPROVED  # not disbursed


def test_checker_approval_executes_action(coop, approved_loan):
    maker = _officer(coop, "maker@i.co")
    checker = _officer(coop, "checker@i.co")
    aid = _client(maker).post(
        "/api/v1/approvals/",
        {"action": "loan.disburse", "object_id": approved_loan.id},
        format="json").json()["id"]

    resp = _client(checker).post(f"/api/v1/approvals/{aid}/approve/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    approved_loan.refresh_from_db()
    assert approved_loan.status == Loan.Status.DISBURSED  # executed on approval


def test_reject_does_not_execute(coop, approved_loan):
    maker = _officer(coop, "maker@i.co")
    checker = _officer(coop, "checker@i.co")
    aid = _client(maker).post(
        "/api/v1/approvals/",
        {"action": "loan.disburse", "object_id": approved_loan.id},
        format="json").json()["id"]
    resp = _client(checker).post(f"/api/v1/approvals/{aid}/reject/")
    assert resp.json()["status"] == "rejected"
    approved_loan.refresh_from_db()
    assert approved_loan.status == Loan.Status.APPROVED


# ── One-person societies ────────────────────────────────────────────────────
def _officer_as(coop, email, slug):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name=email, password="x")
        Membership.objects.create(user=u, member_no=email[:6],
                                  role=Role.objects.filter(slug=slug).first())
    return u


def _submit(client, loan):
    r = client.post("/api/v1/approvals/",
                    {"action": "loan.disburse", "object_id": loan.id},
                    format="json")
    assert r.status_code == 201, r.content
    return r.json()["id"]


def test_sole_secretary_may_approve_own_request(coop, approved_loan):
    """With nobody else to check, dual control would block the society forever."""
    from audit.models import AuditLog

    secretary = _officer(coop, "solo@i.co")
    client = _client(secretary)
    assert client.get("/api/v1/approvals/policy/").json() == {
        "sole_secretary": True}

    aid = _submit(client, approved_loan)
    resp = client.post(f"/api/v1/approvals/{aid}/approve/")

    assert resp.status_code == 200, resp.content
    assert resp.json()["self_decided"] is True
    approved_loan.refresh_from_db()
    assert approved_loan.status == Loan.Status.DISBURSED
    # Nobody else checked it, so the audit log records that plainly.
    assert AuditLog.all_objects.filter(
        cooperative=coop, action="approval.self_decided").exists()


def test_exemption_ends_when_a_second_officer_is_added(coop, approved_loan):
    secretary = _officer(coop, "solo@i.co")
    client = _client(secretary)
    aid = _submit(client, approved_loan)

    _officer_as(coop, "treasurer@i.co", "treasurer")

    assert client.get("/api/v1/approvals/policy/").json() == {
        "sole_secretary": False}
    assert client.post(f"/api/v1/approvals/{aid}/approve/").status_code == 400


def test_a_chairperson_counts_as_a_second_officer(coop, approved_loan):
    """Any office-holder can check — not only privileged roles."""
    secretary = _officer(coop, "solo@i.co")
    _officer_as(coop, "chair@i.co", "chairperson")
    client = _client(secretary)
    aid = _submit(client, approved_loan)

    assert client.post(f"/api/v1/approvals/{aid}/approve/").status_code == 400


def test_a_sole_officer_who_is_not_secretary_gets_no_exemption(
        coop, approved_loan):
    treasurer = _officer_as(coop, "solo@i.co", "treasurer")
    client = _client(treasurer)
    aid = _submit(client, approved_loan)

    assert client.post(f"/api/v1/approvals/{aid}/approve/").status_code == 400


def test_a_suspended_officer_does_not_count(coop, approved_loan):
    secretary = _officer(coop, "solo@i.co")
    gone = _officer_as(coop, "gone@i.co", "treasurer")
    Membership.all_objects.filter(user=gone).update(
        status=Membership.Status.SUSPENDED)
    client = _client(secretary)
    aid = _submit(client, approved_loan)

    assert client.post(f"/api/v1/approvals/{aid}/approve/").status_code == 200
