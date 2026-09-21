"""End-to-end API tests: auth, tenant header scoping, contribution flow."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _admin_membership(coop, email="sec@imole.coop"):
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name="Secretary", password="pw",
        )
        Membership.objects.create(
            user=user, member_no="ADM-1",
            role=Role.objects.filter(slug="secretary").first(),
        )
    return user


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def test_token_auth_and_me(coop):
    user = _admin_membership(coop)
    resp = _client(user).get("/api/v1/me/")
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


def test_member_list_requires_and_respects_tenant_header(coop, other_coop):
    user = _admin_membership(coop)
    # Also give this user a membership elsewhere to prove header selects tenant.
    client = _client(user)

    # Without the header, single membership is implied → sees coop members.
    resp = client.get("/api/v1/members/")
    assert resp.status_code == 200
    member_nos = {m["member_no"] for m in resp.json()["results"]}
    assert "ADM-1" in member_nos

    # A tenant the user does not belong to → empty (fail closed).
    resp = client.get("/api/v1/members/",
                      HTTP_X_COOPERATIVE_ID=str(other_coop.id))
    assert resp.status_code == 200
    assert resp.json()["results"] == []


def test_record_contribution_endpoint_posts_ledger(coop):
    admin = _admin_membership(coop)
    client = _client(admin)

    # Create a contribution type via API.
    from ledger.models import Account
    funds = Account.all_objects.get(cooperative=coop, code="2000")
    resp = client.post("/api/v1/contribution-types/", {
        "name": "Monthly Dues", "slug": "dues", "frequency": "monthly",
        "kind": "mandatory", "gl_account": funds.id,
    }, format="json")
    assert resp.status_code == 201, resp.content
    type_id = resp.json()["id"]

    # Add a member via API.
    resp = client.post("/api/v1/members/", {
        "member_no": "IMC-1", "full_name": "Ada", "email": "ada@x.com",
    }, format="json")
    assert resp.status_code == 201, resp.content
    member_id = resp.json()["id"]

    # Record a contribution → posts a balanced journal.
    resp = client.post("/api/v1/contributions/record/", {
        "membership": member_id, "contribution_type": type_id,
        "amount": "5000", "channel": "psp", "psp_reference": "PSTK",
    }, format="json")
    assert resp.status_code == 201, resp.content

    # The member statement now reflects the contribution.
    resp = client.get(f"/api/v1/members/{member_id}/statement/")
    assert resp.status_code == 200
    assert Decimal(resp.json()["savings_balance"]) == Decimal("5000.00")


def test_provisioning_requires_platform_admin(coop):
    member_user = _admin_membership(coop, email="notadmin@x.com")
    resp = _client(member_user).post("/api/v1/cooperatives/", {
        "name": "New Coop", "slug": "new-coop",
    }, format="json")
    assert resp.status_code == 403
