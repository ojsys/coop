"""
Activity analytics and the admin-editable Google Analytics ID.

Two surfaces: platform-wide (`/platform/site-activity/`, behind IsPlatformAdmin)
and per-cooperative (`/reports/activity/`, tenant-scoped). The scoping test is
the important one — an activity feed that leaked another cooperative's audit
trail would be a tenancy breach, not a cosmetic bug.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db

ACTIVITY_URL = "/api/v1/platform/site-activity/"
COOP_ACTIVITY_URL = "/api/v1/reports/activity/"
BRANDING_URL = "/api/v1/public/branding/"


@pytest.fixture
def platform_admin():
    return User.objects.create_superuser(
        email="admin@startupripple.co", full_name="Platform Admin",
        password="x",
    )


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _official(coop, email="sec@imole.coop"):
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name="Sec", password="x")
        Membership.objects.create(
            user=user, member_no="ADM-9",
            role=Role.objects.filter(slug="secretary").first())
    return user


# ── The admin-editable GA measurement ID ────────────────────────────────────
def test_public_branding_exposes_the_ga_measurement_id(db):
    resp = APIClient().get(BRANDING_URL)

    assert resp.status_code == 200
    assert "ga_measurement_id" in resp.json()


def test_ga_measurement_id_is_blank_until_an_admin_sets_it(db):
    """Blank is meaningful: the site must load no tracking script at all."""
    from platform_admin.models import PlatformProfile

    assert APIClient().get(BRANDING_URL).json()["ga_measurement_id"] == ""

    profile = PlatformProfile.load()
    profile.ga_measurement_id = "G-TEST12345"
    profile.save(update_fields=["ga_measurement_id"])

    assert APIClient().get(BRANDING_URL).json()["ga_measurement_id"] == "G-TEST12345"


# ── Platform-wide activity ──────────────────────────────────────────────────
def test_site_activity_requires_a_platform_admin(coop):
    """A cooperative officer is not a platform admin."""
    official = _official(coop)

    assert _client(official).get(ACTIVITY_URL).status_code == 403


def test_site_activity_returns_the_documented_shape(coop, platform_admin):
    resp = _client(platform_admin).get(ACTIVITY_URL)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    for key in ("window_days", "totals", "daily", "by_entity", "feed",
                "busiest_cooperatives", "growth_trend", "activation_funnel",
                "payments"):
        assert key in body, f"missing {key}"
    # One bucket per day in the window, so the chart's x-axis is complete
    # rather than skipping quiet days.
    assert len(body["daily"]) == body["window_days"]


def test_site_activity_window_is_clamped(coop, platform_admin):
    """Mirrors the analytics endpoint's clamp so a hand-typed query cannot ask
    for an unbounded scan."""
    client = _client(platform_admin)

    assert client.get(f"{ACTIVITY_URL}?days=1").json()["window_days"] == 7
    assert client.get(f"{ACTIVITY_URL}?days=999").json()["window_days"] == 90
    assert client.get(f"{ACTIVITY_URL}?days=abc").json()["window_days"] == 30


def test_site_activity_counts_a_new_member(coop, platform_admin):
    _official(coop, email="fresh@imole.coop")

    body = _client(platform_admin).get(ACTIVITY_URL).json()

    assert body["totals"]["new_members"] >= 1


# ── Per-cooperative activity ────────────────────────────────────────────────
def test_cooperative_activity_does_not_leak_another_tenant(coop, other_coop):
    from audit.services import record_action

    official = _official(coop)
    record_action(cooperative=other_coop, action="cooperative.suspend")
    record_action(cooperative=coop, action="cooperative.reactivate")

    resp = _client(official).get(COOP_ACTIVITY_URL,
                                 HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200, resp.content
    actions = [row["action"] for row in resp.json()["feed"]]
    assert "cooperative.reactivate" in actions
    assert "cooperative.suspend" not in actions, "another tenant's trail leaked"


def test_cooperative_activity_returns_the_documented_shape(coop):
    official = _official(coop)

    resp = _client(official).get(COOP_ACTIVITY_URL,
                                 HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200, resp.content
    body = resp.json()
    for key in ("window_days", "totals", "daily", "by_entity",
                "top_contributors", "feed"):
        assert key in body, f"missing {key}"
    assert len(body["daily"]) == body["window_days"]
