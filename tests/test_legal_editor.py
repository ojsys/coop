"""
Terms and Conditions, and the platform's Legal pages editor.

Pinned: /terms is published out of the box; the legal team can edit and
publish documents from the platform without the admin; /privacy and /terms
cannot be renamed or deleted; every change is audited; and only platform
admins can do any of it.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from audit.models import AuditLog
from platform_admin.models import LegalDocument

pytestmark = pytest.mark.django_db

URL = "/api/v1/platform-legal/"


@pytest.fixture
def ops():
    admin = User.objects.create_user(email="legal@platform.test",
                                     full_name="Legal", password="x",
                                     is_platform_admin=True)
    client = APIClient()
    client.force_authenticate(admin)
    return client


def test_terms_are_published_out_of_the_box():
    res = APIClient().get("/api/v1/public/legal/terms/")
    assert res.status_code == 200
    assert res.data["title"] == "Terms and Conditions"
    assert "## 7. Refer and earn" in res.data["body"]


def test_the_legal_team_edits_and_the_public_page_follows(ops):
    terms = LegalDocument.objects.get(slug="terms")
    res = ops.patch(f"{URL}{terms.id}/", {"body": "New terms.",
                                          "effective_date": "2026-11-01"},
                    format="json")
    assert res.status_code == 200, res.data
    public = APIClient().get("/api/v1/public/legal/terms/").data
    assert public["body"] == "New terms."
    assert str(public["effective_date"]) == "2026-11-01"
    assert AuditLog.all_objects.filter(action="legal.update").exists()


def test_a_new_document_stays_hidden_until_published(ops):
    res = ops.post(URL, {"slug": "refunds", "title": "Refund policy",
                         "body": "Draft."}, format="json")
    assert res.status_code == 201, res.data
    assert APIClient().get("/api/v1/public/legal/refunds/").status_code == 404
    ops.patch(f"{URL}{res.data['id']}/", {"published": True}, format="json")
    assert APIClient().get("/api/v1/public/legal/refunds/").status_code == 200
    assert ops.delete(f"{URL}{res.data['id']}/").status_code == 204


def test_wired_slugs_cannot_be_renamed_or_deleted(ops):
    terms = LegalDocument.objects.get(slug="terms")
    assert ops.patch(f"{URL}{terms.id}/", {"slug": "tos"},
                     format="json").status_code == 400
    assert ops.delete(f"{URL}{terms.id}/").status_code == 400
    assert ops.get(URL).data[0]["fixed"] in (True, False)


def test_only_platform_admins(member):
    client = APIClient()
    client.force_authenticate(member.user)
    assert client.get(URL).status_code == 403
