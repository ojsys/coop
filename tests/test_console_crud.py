"""Console CRUD additions: member edit/status, society-profile update, roles."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _privileged_user(coop, email="sec@imole.coop"):
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name="Secretary", password="pw")
        Membership.objects.create(
            user=user, member_no="ADM-1",
            role=Role.objects.filter(slug="secretary").first())
    return user


def _plain_member(coop, email="rank@imole.coop"):
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name="Rank File", password="pw")
        Membership.objects.create(
            user=user, member_no="RNK-1",
            role=Role.objects.filter(slug="member").first())
    return user


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


# ── Member edit + status ────────────────────────────────────────────────────
def test_member_edit_updates_user_and_membership(coop):
    admin = _privileged_user(coop)
    client = _client(admin)
    with use_tenant(coop):
        u = User.objects.create_user(email="ada@example.com", full_name="Ada")
        m = Membership.objects.create(user=u, member_no="IMC-9",
                                      share_capital="1000")

    resp = client.patch(f"/api/v1/members/{m.id}/", {
        "full_name": "Ada Okonkwo", "phone": "08099998888",
        "share_capital": "5000",
    }, format="json")
    assert resp.status_code == 200, resp.content
    m.refresh_from_db()
    m.user.refresh_from_db()
    assert m.user.full_name == "Ada Okonkwo"
    assert m.user.phone == "08099998888"
    assert m.share_capital == Decimal("5000")


def test_member_status_change(coop):
    admin = _privileged_user(coop)
    client = _client(admin)
    with use_tenant(coop):
        u = User.objects.create_user(email="b@example.com", full_name="Bala")
        m = Membership.objects.create(user=u, member_no="IMC-10")

    resp = client.patch(f"/api/v1/members/{m.id}/", {"status": "suspended"},
                        format="json")
    assert resp.status_code == 200
    m.refresh_from_db()
    assert m.status == Membership.Status.SUSPENDED


# ── Society-profile update ──────────────────────────────────────────────────
def test_privileged_member_can_edit_society_profile(coop):
    admin = _privileged_user(coop)
    resp = _client(admin).patch(f"/api/v1/cooperatives/{coop.id}/", {
        "name": "Ìmọ̀lè MCS (renamed)", "brand_color": "#123456",
        "state": "Oyo",
    }, format="json")
    assert resp.status_code == 200, resp.content
    coop.refresh_from_db()
    assert coop.name == "Ìmọ̀lè MCS (renamed)"
    assert coop.brand_color == "#123456"
    assert coop.state == "Oyo"
    # slug/status are not editable here.
    assert coop.slug == "imole"


def test_plain_member_cannot_edit_society_profile(coop):
    member = _plain_member(coop)
    resp = _client(member).patch(f"/api/v1/cooperatives/{coop.id}/",
                                 {"name": "Hacked"}, format="json")
    assert resp.status_code == 403
    coop.refresh_from_db()
    assert coop.name != "Hacked"


# ── Role CRUD ───────────────────────────────────────────────────────────────
def test_role_crud(coop):
    admin = _privileged_user(coop)
    client = _client(admin)

    # Create
    resp = client.post("/api/v1/roles/", {
        "slug": "auditor", "name": "Auditor",
        "permissions": ["reports.view"],
    }, format="json")
    assert resp.status_code == 201, resp.content
    role_id = resp.json()["id"]

    # Update (grant a privileged permission → is_privileged flips)
    resp = client.patch(f"/api/v1/roles/{role_id}/",
                        {"permissions": ["reports.view", "ledger.reconcile"]},
                        format="json")
    assert resp.status_code == 200
    assert resp.json()["is_privileged"] is True

    # Delete (unassigned) succeeds
    assert client.delete(f"/api/v1/roles/{role_id}/").status_code == 204


def test_cannot_delete_role_in_use(coop):
    admin = _privileged_user(coop)  # has the 'secretary' role assigned
    client = _client(admin)
    with use_tenant(coop):
        secretary = Role.objects.get(slug="secretary")
    resp = client.delete(f"/api/v1/roles/{secretary.id}/")
    assert resp.status_code == 409


def test_plain_member_cannot_write_roles(coop):
    member = _plain_member(coop)
    resp = _client(member).post("/api/v1/roles/", {
        "slug": "x", "name": "X", "permissions": [],
    }, format="json")
    assert resp.status_code == 403
