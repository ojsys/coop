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
