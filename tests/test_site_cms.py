"""The public-site CMS.

The point of these tables is that a non-technical admin can change what the
marketing site says without a developer, a build or a deploy. So the tests
care about two things: the content is readable by anonymous visitors, and what
an admin does in the Django admin actually reaches the page.
"""
from __future__ import annotations

import base64

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from platform_admin.models import (
    SiteContent, SiteFeature, SiteGalleryImage, SiteStep, SiteTrustBadge,
)

pytestmark = pytest.mark.django_db

URL = "/api/v1/public/site-content/"

# Smallest valid PNG — ImageField wants real image bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def media(settings, tmp_path):
    """Keep uploaded test images out of the real media directory."""
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def image(name="photo.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


@pytest.fixture
def blank_cms():
    """Start from an empty CMS.

    Migration 0007 seeds the live copy — correct in production, but it means
    a test that does not clear it is asserting against six seeded features it
    did not create. Tests that care about ordering or emptiness take this so
    they own their own data.
    """
    SiteContent.objects.all().delete()
    SiteFeature.objects.all().delete()
    SiteStep.objects.all().delete()
    SiteTrustBadge.objects.all().delete()
    SiteGalleryImage.objects.all().delete()


# ── Reachability ────────────────────────────────────────────────────────────
def test_content_is_readable_without_a_token():
    resp = APIClient().get(URL)
    assert resp.status_code == 200
    assert resp.json()["hero"]["title"]


def test_reading_the_page_does_not_write_a_row(blank_cms):
    """An anonymous GET must not create data.

    The view returns an unsaved instance when no row exists, so the defaults
    still render. Using load() here would mean every visit to the marketing
    site wrote to the database.
    """
    assert SiteContent.objects.count() == 0
    resp = APIClient().get(URL)

    assert resp.status_code == 200
    assert resp.json()["hero"]["title"], "defaults should still be served"
    assert SiteContent.objects.count() == 0, "a GET must not create a row"


# ── Editing reaches the page ────────────────────────────────────────────────
def test_the_cms_ships_with_its_content_already_filled_in():
    """Seeded, not empty.

    An editor opening Site content for the first time should find the live
    wording to edit. Empty tables would give them blank boxes and no idea
    what the page currently says.
    """
    assert SiteFeature.objects.exists(), "migration 0007 should seed features"
    assert SiteStep.objects.exists(), "migration 0007 should seed steps"
    assert SiteTrustBadge.objects.exists(), "migration 0007 should seed badges"

    body = APIClient().get(URL).json()
    assert len(body["features"]) >= 6
    assert len(body["steps"]) >= 3
    assert len(body["trust_badges"]) >= 4


def test_seeded_trust_badges_are_not_tied_to_one_jurisdiction():
    """The data-protection line named Nigeria's NDPA; it must not any more."""
    texts = " ".join(
        SiteTrustBadge.objects.values_list("text", flat=True)).lower()
    assert "ndpa" not in texts
    assert "data-protection" in texts or "data protection" in texts


def test_editing_the_headline_changes_the_payload():
    content = SiteContent.load()
    content.hero_title = "Cooperatives, run properly."
    content.hero_subtitle = "A new subtitle."
    content.save()

    hero = APIClient().get(URL).json()["hero"]
    assert hero["title"] == "Cooperatives, run properly."
    assert hero["subtitle"] == "A new subtitle."


def test_default_copy_is_not_region_specific():
    """The site should not read as though it serves one continent."""
    body = APIClient().get(URL).json()
    blob = " ".join([
        body["hero"]["eyebrow"], body["hero"]["title"],
        body["hero"]["subtitle"], body["principle"]["body"],
    ]).lower()
    assert "african" not in blob
    assert "africa" not in blob


# ── Ordered, hideable lists ─────────────────────────────────────────────────
def test_features_follow_their_order(blank_cms):
    SiteFeature.objects.create(order=2, title="Third", body="c")
    SiteFeature.objects.create(order=0, title="First", body="a")
    SiteFeature.objects.create(order=1, title="Second", body="b")

    titles = [f["title"] for f in APIClient().get(URL).json()["features"]]
    assert titles == ["First", "Second", "Third"]


def test_hidden_items_are_withheld_from_the_site(blank_cms):
    """Unticking 'visible' takes it off the page without losing the text."""
    SiteFeature.objects.create(order=0, title="Shown", body="a")
    SiteFeature.objects.create(order=1, title="Hidden", body="b",
                               visible=False)
    SiteStep.objects.create(order=0, number="01", title="Live", body="a")
    SiteStep.objects.create(order=1, number="02", title="Draft", body="b",
                            visible=False)
    SiteTrustBadge.objects.create(order=0, text="Kept")
    SiteTrustBadge.objects.create(order=1, text="Gone", visible=False)

    body = APIClient().get(URL).json()
    assert [f["title"] for f in body["features"]] == ["Shown"]
    assert [s["title"] for s in body["steps"]] == ["Live"]
    assert [b["text"] for b in body["trust_badges"]] == ["Kept"]
    # Still on file, just not published.
    assert SiteFeature.objects.count() == 2


def test_trust_badges_carry_their_icon(blank_cms):
    SiteTrustBadge.objects.create(order=0, text="Audited", icon="fact_check")
    badge = APIClient().get(URL).json()["trust_badges"][0]
    assert badge == {"icon": "fact_check", "text": "Audited"}


# ── Imagery ─────────────────────────────────────────────────────────────────
def test_gallery_exposes_uploaded_photographs(media, blank_cms):
    SiteGalleryImage.objects.create(
        order=0, image=image(), alt_text="Members at a meeting",
        caption="Monthly contribution day",
    )

    gallery = APIClient().get(URL).json()["gallery"]
    assert len(gallery) == 1
    assert gallery[0]["image"], "an uploaded image must expose a URL"
    assert gallery[0]["alt_text"] == "Members at a meeting"
    assert gallery[0]["caption"] == "Monthly contribution day"


def test_hero_image_is_null_until_one_is_uploaded():
    assert APIClient().get(URL).json()["hero"]["image"] is None


def test_hero_image_is_served_once_uploaded(media):
    content = SiteContent.load()
    content.hero_image = image("hero.png")
    content.hero_image_alt = "A savings group counting contributions"
    content.save()

    hero = APIClient().get(URL).json()["hero"]
    assert hero["image"]
    assert hero["image_alt"] == "A savings group counting contributions"
