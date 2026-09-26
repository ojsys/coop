"""Console CRUD additions: member edit/status, society-profile update, roles."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _privileged_user(coop, email="sec@imole.coop", member_no="ADM-1"):
    # member_no is unique per cooperative, so a second officer needs its own.
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name="Secretary", password="pw")
        Membership.objects.create(
            user=user, member_no=member_no,
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


# ── Membership writes are privileged-only ───────────────────────────────────
# MembershipSerializer exposes role, status, member_no and share_capital as
# writable. The viewset was IsAuthenticated alone, so any member could PATCH
# their own membership onto a privileged role and take the cooperative over.
# Proven reproducible before the fix; these keep it shut.
def _membership_of(user, coop):
    from accounts.models import Membership

    return Membership.all_objects.get(user=user, cooperative=coop)


def test_plain_member_cannot_promote_themselves(coop):
    member = _plain_member(coop)
    mine = _membership_of(member, coop)
    with use_tenant(coop):
        secretary = Role.objects.get(slug="secretary")

    resp = _client(member).patch(f"/api/v1/members/{mine.id}/",
                                 {"role": secretary.id}, format="json")

    assert resp.status_code == 403
    mine.refresh_from_db()
    assert mine.role.is_privileged is False, "a member escalated their own role"


def test_plain_member_cannot_edit_another_member(coop):
    member = _plain_member(coop)
    admin = _privileged_user(coop)
    theirs = _membership_of(admin, coop)

    resp = _client(member).patch(f"/api/v1/members/{theirs.id}/",
                                 {"share_capital": "999999"}, format="json")

    assert resp.status_code == 403
    theirs.refresh_from_db()
    assert str(theirs.share_capital) != "999999.00"


def test_plain_member_cannot_demote_an_officer(coop):
    """The mirror image: locking the officers out is as bad as self-promotion."""
    member = _plain_member(coop)
    admin = _privileged_user(coop)
    theirs = _membership_of(admin, coop)
    with use_tenant(coop):
        plain = Role.objects.get(slug="member")

    resp = _client(member).patch(f"/api/v1/members/{theirs.id}/",
                                 {"role": plain.id}, format="json")

    assert resp.status_code == 403
    theirs.refresh_from_db()
    assert theirs.role.is_privileged is True


def test_plain_member_may_still_read_the_member_list(coop):
    """Reads stay open — the console depends on them."""
    member = _plain_member(coop)
    resp = _client(member).get("/api/v1/members/")
    assert resp.status_code == 200


# ── The last officer cannot be removed ──────────────────────────────────────
# Admin is meant to be revocable, but a cooperative with nobody holding
# members.manage can no longer admit members or hand the role on — the dead
# end this whole change exists to eliminate.
def test_the_only_officer_cannot_demote_themselves(coop):
    admin = _privileged_user(coop)
    mine = _membership_of(admin, coop)
    with use_tenant(coop):
        plain = Role.objects.get(slug="member")

    resp = _client(admin).patch(f"/api/v1/members/{mine.id}/",
                                {"role": plain.id}, format="json")

    assert resp.status_code == 400, resp.content
    mine.refresh_from_db()
    assert mine.role.is_privileged is True


def test_the_only_officer_cannot_deactivate_themselves(coop):
    admin = _privileged_user(coop)
    mine = _membership_of(admin, coop)

    resp = _client(admin).patch(f"/api/v1/members/{mine.id}/",
                                {"status": "exited"}, format="json")

    assert resp.status_code == 400, resp.content
    mine.refresh_from_db()
    assert mine.status == "active"


def test_an_officer_may_step_down_once_another_exists(coop):
    """Revocable, as asked — just never down to zero."""
    first = _privileged_user(coop)
    second = _privileged_user(coop, email="second@imole.coop",
                              member_no="ADM-2")
    mine = _membership_of(first, coop)
    with use_tenant(coop):
        plain = Role.objects.get(slug="member")

    resp = _client(second).patch(f"/api/v1/members/{mine.id}/",
                                 {"role": plain.id}, format="json")

    assert resp.status_code == 200, resp.content
    mine.refresh_from_db()
    assert mine.role.slug == "member"


def test_clearing_the_only_officers_role_is_also_refused(coop):
    """Explicitly nulling the role is the same demotion by another route."""
    admin = _privileged_user(coop)
    mine = _membership_of(admin, coop)

    resp = _client(admin).patch(f"/api/v1/members/{mine.id}/",
                                {"role": None}, format="json")

    assert resp.status_code == 400, resp.content
    mine.refresh_from_db()
    assert mine.role is not None and mine.role.is_privileged


def test_a_privileged_member_may_still_assign_roles(coop):
    """The fix must not break the officers it exists to protect."""
    admin = _privileged_user(coop)
    other = _plain_member(coop, email="other@imole.coop")
    theirs = _membership_of(other, coop)
    with use_tenant(coop):
        treasurer = Role.objects.get(slug="treasurer")

    resp = _client(admin).patch(f"/api/v1/members/{theirs.id}/",
                                {"role": treasurer.id}, format="json")

    assert resp.status_code == 200, resp.content
    theirs.refresh_from_db()
    assert theirs.role.slug == "treasurer"


# ── Role permission edits cannot empty a cooperative of officers ────────────
# is_privileged is *derived* from Role.permissions, so unticking "Manage
# members" in the role editor demotes every holder at once. The membership
# guard above never sees that edit — same dead end, different door.
def _treasurer_user(coop, email="tre@imole.coop"):
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name="Treasurer", password="pw")
        Membership.objects.create(
            user=user, member_no="TRE-1",
            role=Role.objects.filter(slug="treasurer").first())
    return user


def test_role_edit_cannot_strip_the_last_officers_privilege(coop):
    admin = _privileged_user(coop)  # holds 'secretary'
    with use_tenant(coop):
        secretary = Role.objects.get(slug="secretary")

    resp = _client(admin).patch(f"/api/v1/roles/{secretary.id}/",
                                {"permissions": ["reports.view"]},
                                format="json")

    assert resp.status_code == 400, resp.content
    secretary.refresh_from_db()
    assert secretary.is_privileged, "the role editor demoted the last officer"


def test_role_edit_may_drop_privilege_when_another_officer_remains(coop):
    admin = _privileged_user(coop)
    _treasurer_user(coop)  # an officer by a different role
    with use_tenant(coop):
        secretary = Role.objects.get(slug="secretary")

    resp = _client(admin).patch(f"/api/v1/roles/{secretary.id}/",
                                {"permissions": ["reports.view"]},
                                format="json")

    assert resp.status_code == 200, resp.content
    secretary.refresh_from_db()
    assert secretary.is_privileged is False


def test_unheld_privileged_role_may_be_stripped(coop):
    """Nobody holds Treasurer here, so nobody is demoted by the edit."""
    admin = _privileged_user(coop)
    with use_tenant(coop):
        treasurer = Role.objects.get(slug="treasurer")

    resp = _client(admin).patch(f"/api/v1/roles/{treasurer.id}/",
                                {"permissions": ["reports.view"]},
                                format="json")

    assert resp.status_code == 200, resp.content


def test_role_edit_that_keeps_privilege_is_untouched(coop):
    """The guard must not block ordinary permission tuning."""
    admin = _privileged_user(coop)
    with use_tenant(coop):
        secretary = Role.objects.get(slug="secretary")

    resp = _client(admin).patch(
        f"/api/v1/roles/{secretary.id}/",
        {"permissions": ["members.manage", "reports.view"]}, format="json")

    assert resp.status_code == 200, resp.content
    secretary.refresh_from_db()
    assert secretary.is_privileged
