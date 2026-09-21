"""Phase D: richer analytics — ranges, revenue, funnel, cohort, CSV export."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from platform_admin.models import Plan, Subscription
from platform_admin.services import analytics, mrr_trend

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


@pytest.fixture
def subscribed(coop):
    plan = Plan.objects.create(name="Growth", tier=Plan.Tier.MEDIUM,
                               price_monthly=Decimal("35000"))
    Subscription.objects.create(cooperative=coop, plan=plan,
                                status=Subscription.Status.ACTIVE)
    return coop


def test_analytics_shape_and_range(api, subscribed):
    resp = api.get("/api/v1/platform/analytics/?months=12")
    assert resp.status_code == 200
    body = resp.json()
    assert body["months"] == 12
    assert len(body["mrr_trend"]) == 12
    assert len(body["growth_trend"]) == 12
    for key in ["revenue_by_tier", "arpu", "paid_vs_trial",
                "activation_funnel", "active_vs_dormant", "cohort_retention"]:
        assert key in body


def test_months_param_is_clamped(api):
    assert api.get("/api/v1/platform/analytics/?months=1").json()["months"] == 3
    assert api.get("/api/v1/platform/analytics/?months=99").json()["months"] == 24
    assert api.get("/api/v1/platform/analytics/?months=abc").json()["months"] == 6


def test_mrr_and_revenue_reflect_subscriptions(subscribed):
    assert mrr_trend(3)[-1]["mrr"] == Decimal("35000")
    a = analytics(3)
    tiers = {r["tier"]: r["revenue"] for r in a["revenue_by_tier"]}
    assert tiers["medium"] == Decimal("35000")
    assert a["paid_vs_trial"]["paid"] == 1


def test_activation_funnel_is_monotonic(api, subscribed):
    funnel = api.get("/api/v1/platform/analytics/").json()["activation_funnel"]
    counts = [s["count"] for s in funnel]
    assert counts == sorted(counts, reverse=True)  # never widens down the funnel


def test_csv_export(api, subscribed):
    resp = api.get("/api/v1/platform/analytics/export/?months=6")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    text = resp.content.decode()
    assert text.splitlines()[0] == "period,mrr,new_coops,new_members"


def test_analytics_requires_platform_admin(coop):
    from accounts.models import Membership, Role
    from core.context import use_tenant
    with use_tenant(coop):
        u = User.objects.create_user(email="m@i.co", full_name="M", password="x")
        Membership.objects.create(user=u, member_no="M-1",
                                  role=Role.objects.filter(slug="member").first())
    c = APIClient()
    c.force_authenticate(u)
    assert c.get("/api/v1/platform/analytics/").status_code == 403
