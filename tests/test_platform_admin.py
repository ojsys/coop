"""Platform-ops CRUD + computed endpoints (Startup Ripple surface)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.models import Membership, User
from platform_admin.models import (
    Domain, Invoice, OnboardingItem, Plan, PlatformTeamMember, Subscription,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return User.objects.create_superuser(
        email="admin@startupripple.co", full_name="Platform Admin",
        password="x",
    )


@pytest.fixture
def api(admin):
    client = APIClient()
    client.force_authenticate(admin)
    return client


@pytest.fixture
def plan():
    return Plan.objects.create(
        name="Growth", tier=Plan.Tier.MEDIUM, price_monthly=Decimal("35000"),
        min_members=500, max_members=5000,
    )


# ── Permission gating ───────────────────────────────────────────────────────
def test_platform_endpoints_reject_non_admin(coop):
    client = APIClient()
    with __import__("core.context", fromlist=["use_tenant"]).use_tenant(coop):
        member_user = User.objects.create_user(
            email="sec@imole.coop", full_name="Sec", password="x")
        Membership.objects.create(user=member_user, member_no="IMC-0002")
    client.force_authenticate(member_user)
    for url in ["/api/v1/plans/", "/api/v1/invoices/", "/api/v1/domains/",
                "/api/v1/platform/billing/", "/api/v1/platform/health/"]:
        assert client.get(url).status_code == 403, url


# ── Plan CRUD ───────────────────────────────────────────────────────────────
def test_plan_crud(api):
    # Create
    resp = api.post("/api/v1/plans/", {
        "name": "Starter", "tier": "small", "price_monthly": "10000",
        "min_members": 50, "max_members": 500,
    }, format="json")
    assert resp.status_code == 201, resp.content
    plan_id = resp.data["id"]

    # List
    resp = api.get("/api/v1/plans/")
    assert resp.status_code == 200
    assert any(p["id"] == plan_id for p in _rows(resp))

    # Update
    resp = api.patch(f"/api/v1/plans/{plan_id}/", {"price_monthly": "12000"},
                     format="json")
    assert resp.status_code == 200
    assert Decimal(resp.data["price_monthly"]) == Decimal("12000")

    # Delete
    assert api.delete(f"/api/v1/plans/{plan_id}/").status_code == 204
    assert not Plan.objects.filter(id=plan_id).exists()


# ── Subscription + Invoice mark-paid ────────────────────────────────────────
def test_invoice_mark_paid(api, coop, plan):
    sub = Subscription.objects.create(cooperative=coop, plan=plan,
                                      status=Subscription.Status.ACTIVE)
    inv = Invoice.objects.create(
        cooperative=coop, subscription=sub, number="INV-TEST-1",
        amount=Decimal("35000"), status=Invoice.Status.OVERDUE)

    resp = api.post(f"/api/v1/invoices/{inv.id}/mark-paid/")
    assert resp.status_code == 200
    inv.refresh_from_db()
    assert inv.status == Invoice.Status.PAID
    assert inv.paid_at is not None


# ── Onboarding advance ──────────────────────────────────────────────────────
def test_onboarding_advance(api):
    item = OnboardingItem.objects.create(
        prospect_name="Kano Millers Union",
        stage=OnboardingItem.Stage.DISCOVERY)

    resp = api.post(f"/api/v1/onboarding-items/{item.id}/advance/")
    assert resp.status_code == 200
    assert resp.data["stage"] == OnboardingItem.Stage.DATA_MIGRATION
    assert resp.data["display_name"] == "Kano Millers Union"


# ── Domain verify ───────────────────────────────────────────────────────────
def test_domain_verify(api, coop):
    dom = Domain.objects.create(cooperative=coop, domain="my.imole.coop")
    assert dom.dns_status == Domain.DNSStatus.PENDING

    resp = api.post(f"/api/v1/domains/{dom.id}/verify/")
    assert resp.status_code == 200
    dom.refresh_from_db()
    assert dom.dns_status == Domain.DNSStatus.VERIFIED
    assert dom.ssl_status == Domain.SSLStatus.ISSUED
    assert dom.verified_at is not None


# ── Platform team invite ────────────────────────────────────────────────────
def test_platform_team_invite_creates_admin_user(api):
    resp = api.post("/api/v1/platform-team/", {
        "invite_email": "hauwa@startupripple.co", "invite_name": "Hauwa Bello",
        "role": "onboarding_lead",
    }, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.data["email"] == "hauwa@startupripple.co"
    assert resp.data["name"] == "Hauwa Bello"

    invited = User.objects.get(email="hauwa@startupripple.co")
    assert invited.is_platform_admin is True
    assert PlatformTeamMember.objects.filter(user=invited).exists()


# ── Computed endpoints ──────────────────────────────────────────────────────
def test_billing_summary_endpoint(api, coop, plan):
    Subscription.objects.create(cooperative=coop, plan=plan,
                                status=Subscription.Status.ACTIVE)
    Invoice.objects.create(cooperative=coop, number="INV-OD-1",
                           amount=Decimal("10000"),
                           status=Invoice.Status.OVERDUE)

    resp = api.get("/api/v1/platform/billing/")
    assert resp.status_code == 200
    assert Decimal(str(resp.data["mrr"])) == Decimal("35000")
    assert Decimal(str(resp.data["overdue_amount"])) == Decimal("10000")
    assert resp.data["overdue_count"] == 1


def test_analytics_and_attention_and_health(api, coop, plan):
    Invoice.objects.create(cooperative=coop, number="INV-OD-2",
                           amount=Decimal("10000"),
                           status=Invoice.Status.OVERDUE)

    analytics = api.get("/api/v1/platform/analytics/")
    assert analytics.status_code == 200
    assert "feature_adoption" in analytics.data
    assert "churn_watchlist" in analytics.data

    attention = api.get("/api/v1/platform/attention/")
    assert attention.status_code == 200
    titles = " ".join(i["title"] for i in attention.data["items"])
    assert "overdue" in titles.lower()

    health = api.get("/api/v1/platform/health/")
    assert health.status_code == 200
    assert "providers" in health.data and "system" in health.data


def _rows(resp):
    """Unwrap a possibly-paginated list response."""
    data = resp.data
    return data["results"] if isinstance(data, dict) and "results" in data \
        else data
