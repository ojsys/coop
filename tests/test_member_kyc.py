"""Phase 1: member KYC fields, headshot upload, KYC documents."""
from __future__ import annotations

import io

import pytest
from PIL import Image
from rest_framework.test import APIClient

from accounts.models import MemberDocument, Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)


def _secretary(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="sec@imole.coop", full_name="Sec",
                                     password="x")
        Membership.objects.create(user=u, member_no="ADM-1",
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _member(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="ada@x.co", full_name="Ada")
        return Membership.objects.create(user=u, member_no="IMC-9")


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (11, 79, 58)).save(buf, format="PNG")
    return buf.getvalue()


def test_member_kyc_fields_update(coop):
    client = _client(_secretary(coop))
    m = _member(coop)
    resp = client.patch(f"/api/v1/members/{m.id}/", {
        "gender": "female", "occupation": "Trader", "address": "12 Balogun St",
        "next_of_kin_name": "Chidi", "bank_name": "GTBank",
        "bank_account_no": "0123456789", "date_of_birth": "1990-05-01",
    }, format="json")
    assert resp.status_code == 200, resp.content
    m.refresh_from_db()
    assert m.gender == "female"
    assert m.occupation == "Trader"
    assert m.bank_account_no == "0123456789"
    assert str(m.date_of_birth) == "1990-05-01"


def test_member_photo_upload(coop):
    client = _client(_secretary(coop))
    m = _member(coop)
    from django.core.files.uploadedfile import SimpleUploadedFile
    photo = SimpleUploadedFile("me.png", _png_bytes(), content_type="image/png")
    resp = client.patch(f"/api/v1/members/{m.id}/", {"photo": photo},
                        format="multipart")
    assert resp.status_code == 200, resp.content
    m.refresh_from_db()
    assert m.photo  # a file was stored
    assert resp.json()["photo"].endswith(m.photo.name.split("/")[-1]) or \
        "member_photos" in resp.json()["photo"]


def test_member_document_upload_list_delete(coop):
    client = _client(_secretary(coop))
    m = _member(coop)
    from django.core.files.uploadedfile import SimpleUploadedFile
    doc = SimpleUploadedFile("nin.png", _png_bytes(), content_type="image/png")
    resp = client.post("/api/v1/member-documents/", {
        "membership": m.id, "doc_type": "id", "label": "NIN slip", "file": doc,
    }, format="multipart")
    assert resp.status_code == 201, resp.content
    did = resp.json()["id"]

    # Listed under the member; document_count reflects it.
    listed = client.get(f"/api/v1/member-documents/?membership={m.id}")
    rows = listed.json()["results"] if isinstance(listed.json(), dict) else listed.json()
    assert any(r["id"] == did for r in rows)
    assert client.get(f"/api/v1/members/{m.id}/").json()["document_count"] == 1

    assert client.delete(f"/api/v1/member-documents/{did}/").status_code == 204
    assert not MemberDocument.all_objects.filter(id=did).exists()


def test_documents_are_tenant_scoped(coop, other_coop):
    client = _client(_secretary(coop))
    # A document created in `coop` is invisible when the header selects another.
    m = _member(coop)
    from django.core.files.uploadedfile import SimpleUploadedFile
    client.post("/api/v1/member-documents/", {
        "membership": m.id, "doc_type": "id",
        "file": SimpleUploadedFile("x.png", _png_bytes(), content_type="image/png"),
    }, format="multipart")
    resp = client.get("/api/v1/member-documents/",
                      HTTP_X_COOPERATIVE_ID=str(other_coop.id))
    rows = resp.json()["results"] if isinstance(resp.json(), dict) else resp.json()
    assert rows == []
