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


# ── Pasting a Google tag ────────────────────────────────────────────────────
# Google's setup screen gives you a <script> block, not a bare ID, so that is
# what an admin will paste. The ID is lifted out of it rather than the HTML
# being stored and injected into every page.
GTAG_SNIPPET = """
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234XYZ"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-ABC1234XYZ');
</script>
"""


def test_a_bare_measurement_id_is_kept():
    from platform_admin.analytics import extract_measurement_id

    assert extract_measurement_id("G-ABC1234XYZ") == "G-ABC1234XYZ"
    assert extract_measurement_id("  g-abc1234xyz  ") == "G-ABC1234XYZ"


def test_the_id_is_lifted_out_of_a_pasted_snippet():
    from platform_admin.analytics import extract_measurement_id

    assert extract_measurement_id(GTAG_SNIPPET) == "G-ABC1234XYZ"


def test_blank_stays_blank():
    """Blank is meaningful — it switches tracking off."""
    from platform_admin.analytics import extract_measurement_id

    assert extract_measurement_id("") == ""
    assert extract_measurement_id(None) == ""
    assert extract_measurement_id("   ") == ""


def test_a_tag_manager_container_is_refused_with_a_reason():
    """GTM loads a different script; accepting it silently would mean tracking
    that never fires."""
    from platform_admin.analytics import extract_measurement_id

    with pytest.raises(ValueError) as exc:
        extract_measurement_id("GTM-ABC1234")
    assert "Tag Manager" in str(exc.value)


def test_junk_is_refused_with_a_usable_message():
    from platform_admin.analytics import extract_measurement_id

    with pytest.raises(ValueError) as exc:
        extract_measurement_id("my analytics account")
    assert "measurement ID" in str(exc.value)


def test_a_pasted_snippet_normalises_through_the_serializer(db):
    """The console PATCHes /platform/profile/ with whatever was typed, so the
    serializer must accept a snippet far longer than the stored column."""
    from platform_admin.models import PlatformProfile
    from platform_admin.serializers import PlatformProfileSerializer

    serializer = PlatformProfileSerializer(
        PlatformProfile.load(), data={"ga_measurement_id": GTAG_SNIPPET},
        partial=True,
    )
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    assert PlatformProfile.load().ga_measurement_id == "G-ABC1234XYZ"
    assert APIClient().get(BRANDING_URL).json()["ga_measurement_id"] == "G-ABC1234XYZ"


def test_the_serializer_rejects_junk(db):
    from platform_admin.models import PlatformProfile
    from platform_admin.serializers import PlatformProfileSerializer

    serializer = PlatformProfileSerializer(
        PlatformProfile.load(), data={"ga_measurement_id": "nonsense"},
        partial=True,
    )

    assert not serializer.is_valid()
    assert "ga_measurement_id" in serializer.errors


def test_the_admin_form_normalises_a_pasted_snippet(db):
    """Through the admin's form, not full_clean().

    full_clean() runs clean_fields() — and therefore max_length — *before*
    clean(), so an over-length paste can never reach model-level normalisation.
    That is exactly why the form redeclares the field without a length cap.
    """
    from platform_admin.admin import PlatformProfileAdminForm
    from platform_admin.models import PlatformProfile

    profile = PlatformProfile.load()
    form = PlatformProfileAdminForm(
        instance=profile,
        data={
            "name": profile.name,
            "brand_color": profile.brand_color,
            "support_email": profile.support_email,
            "support_phone": profile.support_phone,
            "default_currency": profile.default_currency,
            "default_timezone": profile.default_timezone,
            "ga_measurement_id": GTAG_SNIPPET,
        },
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["ga_measurement_id"] == "G-ABC1234XYZ"
    form.save()
    assert PlatformProfile.load().ga_measurement_id == "G-ABC1234XYZ"


def test_the_admin_form_refuses_a_tag_manager_container(db):
    from platform_admin.admin import PlatformProfileAdminForm
    from platform_admin.models import PlatformProfile

    profile = PlatformProfile.load()
    form = PlatformProfileAdminForm(
        instance=profile,
        data={
            "name": profile.name,
            "brand_color": profile.brand_color,
            "support_email": profile.support_email,
            "support_phone": profile.support_phone,
            "default_currency": profile.default_currency,
            "default_timezone": profile.default_timezone,
            "ga_measurement_id": "GTM-ABC1234",
        },
    )

    assert not form.is_valid()
    assert "Tag Manager" in str(form.errors["ga_measurement_id"])
