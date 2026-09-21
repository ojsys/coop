"""Phase 7: bulk member CSV import."""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _admin(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="sec@imole.coop", full_name="Sec",
                                     password="x")
        Membership.objects.create(user=u, member_no="ADM-1",
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def _csv(text):
    return SimpleUploadedFile("members.csv", text.encode("utf-8"),
                              content_type="text/csv")


def test_bulk_import_creates_and_reports_errors(coop):
    client = _client(_admin(coop))
    csv = (
        "member_no,full_name,email,phone,share_capital,role_slug\n"
        "IMC-100,Ada Nwosu,ada@example.com,08030000000,50000,member\n"
        "IMC-101,Bola Ade,bola@example.com,,10000,\n"
        ",Missing No,noreq@example.com,,0,\n"          # missing member_no -> error
        "IMC-100,Dup,dup@example.com,,0,\n"            # duplicate member_no -> error
    )
    resp = client.post("/api/v1/members/import/", {"file": _csv(csv)},
                       format="multipart")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["created"] == 2
    assert len(body["errors"]) == 2
    rows_with_errors = {e["row"] for e in body["errors"]}
    assert rows_with_errors == {4, 5}
    with use_tenant(coop):
        assert Membership.objects.filter(member_no="IMC-100").count() == 1


def test_import_requires_a_file(coop):
    client = _client(_admin(coop))
    resp = client.post("/api/v1/members/import/", {}, format="multipart")
    assert resp.status_code == 400
