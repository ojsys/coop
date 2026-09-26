"""Platform cooperative ops: lifecycle, impersonation, health, support tickets."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from audit.models import AuditLog
from core.context import use_tenant
from platform_admin.models import SupportTicket
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return User.objects.create_superuser(
        email="admin@startupripple.co", full_name="Platform Admin",
        password="x")


@pytest.fixture
def api(admin):
    client = APIClient()
    client.force_authenticate(admin)
    return client


@pytest.fixture
def secretary(coop):
    with use_tenant(coop):
        user = User.objects.create_user(
            email="sec@imole.coop", full_name="Blessing Okafor", password="x")
        Membership.objects.create(
            user=user, member_no="IMC-1",
            role=Role.objects.filter(slug="secretary").first())
    return user


# ── Lifecycle ───────────────────────────────────────────────────────────────
def test_suspend_reactivate_offboard(api, coop):
    assert api.post(f"/api/v1/cooperatives/{coop.id}/suspend/").status_code == 200
    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.SUSPENDED

    api.post(f"/api/v1/cooperatives/{coop.id}/reactivate/")
    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.ACTIVE

    api.post(f"/api/v1/cooperatives/{coop.id}/offboard/")
    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.CLOSED
    # Lifecycle changes are audit-logged.
    assert AuditLog.all_objects.filter(
        action__startswith="cooperative.").count() >= 3


def test_lifecycle_requires_platform_admin(coop, secretary):
    client = APIClient()
    client.force_authenticate(secretary)
    resp = client.post(f"/api/v1/cooperatives/{coop.id}/suspend/")
    assert resp.status_code == 403


# ── Impersonation ───────────────────────────────────────────────────────────
def test_impersonate_requires_consent(api, coop, secretary):
    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/", {},
                    format="json")
    assert resp.status_code == 400


def test_impersonate_returns_token_and_audits(api, coop, secretary):
    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"consent": True}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["token"]
    assert body["cooperative_id"] == coop.id
    assert body["user"]["email"] == "sec@imole.coop"
    assert AuditLog.all_objects.filter(
        action="cooperative.impersonate").exists()


def test_impersonate_needs_a_privileged_official(api, coop):
    # No privileged member exists → 400.
    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"consent": True}, format="json")
    assert resp.status_code == 400


# ── Impersonating a named member ────────────────────────────────────────────
# Support is usually reproducing one particular person's problem, so the
# platform console needs to open *their* session rather than any officer's.
def _plain_member(coop, email="ada@imole.coop"):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name="Ada Okonkwo")
        return Membership.objects.create(
            user=user, member_no="IMC-77",
            role=Role.objects.filter(slug="member").first())


def test_impersonate_a_named_member(api, coop, secretary):
    membership = _plain_member(coop)

    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"consent": True, "membership": membership.id},
                    format="json")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["user"]["email"] == "ada@imole.coop"
    assert body["user"]["member_no"] == "IMC-77"
    assert body["token"]


def test_impersonating_an_ordinary_member_says_which_surface(api, coop,
                                                             secretary):
    """A plain member has no console; sending them there is a permission wall."""
    membership = _plain_member(coop)

    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"consent": True, "membership": membership.id},
                    format="json")

    assert resp.json()["user"]["is_privileged"] is False


def test_impersonating_an_officer_is_flagged_privileged(api, coop, secretary):
    membership = Membership.all_objects.get(user=secretary, cooperative=coop)

    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"consent": True, "membership": membership.id},
                    format="json")

    assert resp.json()["user"]["is_privileged"] is True


def test_cannot_impersonate_a_member_of_another_cooperative(api, coop,
                                                            other_coop):
    """The membership id is caller-supplied, so it must be checked against
    the cooperative in the URL rather than trusted."""
    with use_tenant(other_coop):
        user = User.objects.create_user(email="outsider@other.coop",
                                        full_name="Outsider")
        foreign = Membership.objects.create(
            user=user, member_no="OTH-1",
            role=Role.objects.filter(slug="secretary").first())

    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"consent": True, "membership": foreign.id},
                    format="json")

    assert resp.status_code == 404, resp.content


def test_impersonation_still_requires_consent_for_a_named_member(api, coop):
    membership = _plain_member(coop)
    resp = api.post(f"/api/v1/cooperatives/{coop.id}/impersonate/",
                    {"membership": membership.id}, format="json")
    assert resp.status_code == 400


# ── Health check ────────────────────────────────────────────────────────────
def test_health_check_shape(api, coop):
    resp = api.get(f"/api/v1/cooperatives/{coop.id}/health-check/")
    assert resp.status_code == 200
    body = resp.json()
    assert "checks" in body and "metrics" in body
    keys = {c["key"] for c in body["checks"]}
    assert {"members", "chart_of_accounts", "ledger_balanced",
            "psp", "reconciled"} <= keys
    # A freshly provisioned coop has a seeded (balanced, empty) ledger.
    assert body["metrics"]["ledger_balanced"] is True


# ── Support tickets ─────────────────────────────────────────────────────────
def test_support_ticket_crud_and_close(api, coop):
    resp = api.post("/api/v1/support-tickets/", {
        "cooperative": coop.id, "subject": "Cannot reconcile June",
        "body": "PSP mismatch", "priority": "high",
    }, format="json")
    assert resp.status_code == 201, resp.content
    tid = resp.json()["id"]
    assert resp.json()["status"] == "open"
    assert resp.json()["created_by_name"] == "Platform Admin"

    # Filter by cooperative
    resp = api.get(f"/api/v1/support-tickets/?cooperative={coop.id}")
    rows = resp.json()["results"] if isinstance(resp.json(), dict) else resp.json()
    assert any(t["id"] == tid for t in rows)

    # Close
    resp = api.post(f"/api/v1/support-tickets/{tid}/close/")
    assert resp.status_code == 200
    ticket = SupportTicket.objects.get(id=tid)
    assert ticket.status == SupportTicket.Status.CLOSED
    assert ticket.closed_at is not None


def test_support_tickets_reject_non_admin(coop, secretary):
    client = APIClient()
    client.force_authenticate(secretary)
    assert client.get("/api/v1/support-tickets/").status_code == 403
