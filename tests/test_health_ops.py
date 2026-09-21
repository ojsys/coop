"""Phase E: fleet health, payment monitor, audit stream, incidents, provider checks."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from platform_admin.models import Incident, ProviderCheck, ProviderStatus
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return User.objects.create_superuser(
        email="admin@startupripple.co", full_name="Platform Admin", password="x")


@pytest.fixture
def api(admin):
    c = APIClient()
    c.force_authenticate(admin)
    return c


def test_fleet_health_rollup(api, coop):
    # A fresh coop with no members/PSP/contributions is "at risk".
    resp = api.get("/api/v1/platform/fleet-health/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert any(t["cooperative_id"] == coop.id for t in body["at_risk"])
    row = next(t for t in body["at_risk"] if t["cooperative_id"] == coop.id)
    assert "failing" in row and row["failing"]


def test_payment_monitor_shape(api):
    resp = api.get("/api/v1/platform/payment-monitor/")
    assert resp.status_code == 200
    body = resp.json()
    assert "trend" in body and "recent" in body
    assert len(body["trend"]) == body["window_days"]


def test_audit_stream_includes_lifecycle(api, coop):
    api.post(f"/api/v1/cooperatives/{coop.id}/suspend/")
    resp = api.get("/api/v1/platform/audit-stream/")
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()["events"]]
    assert "cooperative.suspend" in actions


def test_incident_crud_and_resolve(api):
    resp = api.post("/api/v1/incidents/", {
        "title": "SMS delays", "severity": "major", "note": "termii slow",
    }, format="json")
    assert resp.status_code == 201, resp.content
    iid = resp.json()["id"]
    assert resp.json()["status"] == "open"

    resp = api.post(f"/api/v1/incidents/{iid}/resolve/")
    assert resp.status_code == 200
    inc = Incident.objects.get(id=iid)
    assert inc.status == Incident.Status.RESOLVED
    assert inc.resolved_at is not None


def test_provider_record_appends_history(api):
    provider = ProviderStatus.objects.create(
        name="Paystack", kind=ProviderStatus.Kind.PSP,
        status=ProviderStatus.Status.OPERATIONAL, latency_ms=140)
    assert provider.checks.count() == 0
    resp = api.post(f"/api/v1/provider-status/{provider.id}/record/",
                    {"latency_ms": 200, "status": "degraded"}, format="json")
    assert resp.status_code == 200
    provider.refresh_from_db()
    assert provider.latency_ms == 200
    assert provider.status == "degraded"
    assert ProviderCheck.objects.filter(provider=provider).count() == 1


def test_health_ops_reject_non_admin(coop):
    from accounts.models import Membership, Role
    from core.context import use_tenant
    with use_tenant(coop):
        u = User.objects.create_user(email="m@i.co", full_name="M", password="x")
        Membership.objects.create(user=u, member_no="M-1",
                                  role=Role.objects.filter(slug="member").first())
    c = APIClient()
    c.force_authenticate(u)
    assert c.get("/api/v1/platform/fleet-health/").status_code == 403
    assert c.get("/api/v1/incidents/").status_code == 403
