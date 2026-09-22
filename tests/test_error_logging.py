"""
API error logging.

The gap these close: DRF turns exceptions into responses before Django's
``django.request`` logger runs, so a 400 — a rejected upload, a failed
validation — was previously invisible on the server. The handler must record
every failure *without* changing what the client receives.
"""
from __future__ import annotations

import logging

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from accounts.models import User

pytestmark = pytest.mark.django_db

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
       b'<rect width="10" height="10"/></svg>')


@pytest.fixture(autouse=True)
def _isolated_media(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def platform_client():
    user = User.objects.create_superuser(
        email="ops@startupripple.co", full_name="Ops", password="x")
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_validation_failure_is_logged_with_context(coop, platform_client, caplog):
    """A rejected upload must leave a trace naming the path, status and reason."""
    with caplog.at_level(logging.WARNING, logger="api.errors"):
        resp = platform_client.patch(
            f"/api/v1/cooperatives/{coop.id}/",
            {"logo": SimpleUploadedFile("logo.svg", SVG,
                                        content_type="image/svg+xml")},
            format="multipart",
        )

    assert resp.status_code == 400
    records = [r.getMessage() for r in caplog.records if r.name == "api.errors"]
    assert records, "the 400 was not logged"
    logged = records[0]
    assert "PATCH" in logged
    assert f"/api/v1/cooperatives/{coop.id}/" in logged
    assert "400" in logged
    assert "ops@startupripple.co" in logged          # who hit it
    assert "Upload a valid image" in logged          # why it failed


def test_handler_does_not_change_the_response(coop, platform_client):
    """Logging must be transparent — same status, same body as before."""
    resp = platform_client.patch(
        f"/api/v1/cooperatives/{coop.id}/",
        {"logo": SimpleUploadedFile("logo.svg", SVG,
                                    content_type="image/svg+xml")},
        format="multipart",
    )
    assert resp.status_code == 400
    assert "logo" in resp.data
    assert "Upload a valid image" in str(resp.data["logo"][0])


def test_successful_requests_are_not_logged(coop, platform_client, caplog):
    """The log must stay signal, not traffic."""
    with caplog.at_level(logging.WARNING, logger="api.errors"):
        resp = platform_client.get(f"/api/v1/cooperatives/{coop.id}/")

    assert resp.status_code == 200
    assert [r for r in caplog.records if r.name == "api.errors"] == []


def test_permission_denied_is_logged(coop, caplog):
    """401/403 matter too — they are the other half of 'why did this fail'."""
    with caplog.at_level(logging.WARNING, logger="api.errors"):
        resp = APIClient().get(f"/api/v1/cooperatives/{coop.id}/")

    assert resp.status_code in (401, 403)
    assert [r for r in caplog.records if r.name == "api.errors"], \
        "an unauthenticated rejection should be logged"


def test_non_drf_exception_is_logged_and_left_to_django(caplog):
    """A crash must be recorded, and still become a 500 via Django.

    DRF's handler returns None for anything that isn't an APIException; ours
    must do the same so Django's 500 handling takes over unchanged — but it
    must not pass silently first.
    """
    from core.exception_handler import logging_exception_handler

    with caplog.at_level(logging.ERROR, logger="api.errors"):
        result = logging_exception_handler(
            ValueError("boom"), {"request": None, "view": None})

    assert result is None, "non-DRF exceptions must fall through to Django"
    records = [r for r in caplog.records if r.name == "api.errors"]
    assert records, "the crash was not logged"
    assert "ValueError" in records[0].getMessage()
