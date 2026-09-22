"""
Branding: logos, favicons, contact details and statement headers.

Covers the cooperative's own brand (white-label) and the platform's, plus the
one place branding leaves the screen and lands in a financial document — the
member statement PDF.
"""
from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import User
from platform_admin.models import PlatformProfile
from reports.pdf import member_statement_pdf
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_media(tmp_path, settings):
    """Keep uploaded test files out of the repo's media/ directory.

    Django writes uploads to MEDIA_ROOT immediately — file writes are not part
    of the test transaction, so without this every run leaves real files behind.
    """
    settings.MEDIA_ROOT = tmp_path / "media"


def _png(color=(11, 79, 58), size=(64, 64)) -> SimpleUploadedFile:
    """A real PNG — ImageField validates the payload, so bytes won't do."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return SimpleUploadedFile("logo.png", buffer.getvalue(),
                              content_type="image/png")


@pytest.fixture
def platform_client():
    user = User.objects.create_superuser(
        email="ops@startupripple.co", full_name="Ops", password="x")
    client = APIClient()
    client.force_authenticate(user)
    return client


# ── Cooperative branding ────────────────────────────────────────────────────
def test_logo_uploads_through_the_cooperative_endpoint(coop, platform_client):
    """Multipart PATCH — DRF's default parsers accept the file."""
    resp = platform_client.patch(
        f"/api/v1/cooperatives/{coop.id}/",
        {"logo": _png()},
        format="multipart",
    )
    assert resp.status_code == 200, resp.data
    coop.refresh_from_db()
    assert coop.logo, "logo should be stored on the cooperative"
    assert resp.data["logo"], "the URL should come back for the client to render"


def test_branding_and_contact_fields_round_trip(coop, platform_client):
    resp = platform_client.patch(
        f"/api/v1/cooperatives/{coop.id}/",
        {
            "brand_color": "#123456",
            "contact_email": "hello@imole.coop",
            "contact_phone": "08031234567",
            "contact_address": "12 Awolowo Road, Ikoyi, Lagos",
            "statement_footer": "Imole MCS - RC 12345",
        },
        format="json",
    )
    assert resp.status_code == 200, resp.data
    coop.refresh_from_db()
    assert coop.contact_email == "hello@imole.coop"
    assert coop.statement_footer == "Imole MCS - RC 12345"


def test_cooperative_serializer_exposes_branding(coop, platform_client):
    resp = platform_client.get(f"/api/v1/cooperatives/{coop.id}/")
    assert resp.status_code == 200
    for field in ("logo", "favicon", "contact_email", "contact_phone",
                  "contact_address", "statement_footer"):
        assert field in resp.data, f"{field} missing from the API response"


# ── Platform branding ───────────────────────────────────────────────────────
def test_platform_logo_uploads(platform_client):
    resp = platform_client.patch("/api/v1/platform/profile/",
                                 {"logo": _png()}, format="multipart")
    assert resp.status_code == 200, resp.data
    assert PlatformProfile.load().logo


def test_platform_profile_still_accepts_json(platform_client):
    """The existing JSON path must keep working alongside uploads."""
    resp = platform_client.patch(
        "/api/v1/platform/profile/",
        {"name": "Startup Ripple", "support_phone": "08030000000"},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    profile = PlatformProfile.load()
    assert profile.name == "Startup Ripple"
    assert profile.support_phone == "08030000000"


# ── Statements ──────────────────────────────────────────────────────────────
def test_statement_pdf_renders_with_a_logo(coop, member):
    coop.logo = _png()
    coop.statement_footer = "Imole MCS - RC 12345"
    coop.contact_email = "hello@imole.coop"
    coop.save(update_fields=["logo", "statement_footer", "contact_email"])

    pdf = member_statement_pdf(member)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_statement_pdf_survives_an_unreadable_logo(coop, member):
    """A statement is a financial record — a broken image must not break it.

    Points the field at a path that does not exist on disk, which is what a
    restored database or a moved MEDIA_ROOT looks like.
    """
    Cooperative.objects.filter(pk=coop.pk).update(logo="coop_logos/gone.png")
    coop.refresh_from_db()

    pdf = member_statement_pdf(member)
    assert pdf.startswith(b"%PDF")


def test_statement_pdf_without_any_branding_still_renders(coop, member):
    assert not coop.logo
    pdf = member_statement_pdf(member)
    assert pdf.startswith(b"%PDF")
