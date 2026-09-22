"""
The public endpoints behind the marketing site.

Two things matter here: they must work with no credentials at all (the rest of
the API refuses anonymous callers), and they must not publish operational
detail that belongs to the platform console.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from platform_admin.models import Plan, PlatformProfile

pytestmark = pytest.mark.django_db

BRANDING_URL = "/api/v1/public/branding/"
PLANS_URL = "/api/v1/public/plans/"


@pytest.fixture
def plans():
    small = Plan.objects.create(name="Small", tier=Plan.Tier.SMALL,
                                price_monthly=Decimal("15000.00"),
                                min_members=50, max_members=500)
    large = Plan.objects.create(name="Institutional", tier=Plan.Tier.LARGE,
                                price_monthly=Decimal("75000.00"),
                                min_members=5000, max_members=0)
    retired = Plan.objects.create(name="Legacy", tier=Plan.Tier.MEDIUM,
                                  price_monthly=Decimal("1.00"), active=False)
    return {"small": small, "large": large, "retired": retired}


# ── Reachable without credentials ───────────────────────────────────────────
def test_branding_is_readable_anonymously():
    resp = APIClient().get(BRANDING_URL)
    assert resp.status_code == 200, "the marketing site has no token to send"
    assert "name" in resp.data
    assert "brand_color" in resp.data


def test_plans_are_readable_anonymously(plans):
    resp = APIClient().get(PLANS_URL)
    assert resp.status_code == 200
    assert len(resp.data) == 2


def test_the_rest_of_the_api_still_refuses_anonymous_callers():
    """Guard against the public views loosening the global default."""
    resp = APIClient().get("/api/v1/members/")
    assert resp.status_code in (401, 403)


# ── Only publishable data ───────────────────────────────────────────────────
def test_inactive_plans_are_withheld(plans):
    names = {p["name"] for p in APIClient().get(PLANS_URL).data}
    assert "Legacy" not in names, "retired/draft plans must not be advertised"
    assert names == {"Small", "Institutional"}


def test_plans_do_not_leak_operational_detail(plans):
    """The console serializer carries subscriber counts; the public one must not."""
    plan = APIClient().get(PLANS_URL).data[0]
    for internal in ("subscriber_count", "active", "created_at"):
        assert internal not in plan, f"{internal} should not be published"


def test_plans_are_ordered_cheapest_first(plans):
    prices = [Decimal(p["price_monthly"]) for p in APIClient().get(PLANS_URL).data]
    assert prices == sorted(prices)


def test_branding_reflects_the_platform_profile():
    profile = PlatformProfile.load()
    profile.name = "Startup Ripple"
    profile.support_email = "hello@startupripple.co"
    profile.save(update_fields=["name", "support_email"])

    data = APIClient().get(BRANDING_URL).data
    assert data["name"] == "Startup Ripple"
    assert data["support_email"] == "hello@startupripple.co"
    # Null rather than absent, so the client can branch on it.
    assert "logo" in data and "favicon" in data
