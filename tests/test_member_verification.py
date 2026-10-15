"""
The verified badge and the profile steps that lead to it.

Completing the profile (details, photo, a means of ID) puts a member in the
officers' review queue — it never verifies them by itself, because nothing
checks the uploaded file. Only an officer verifies or declines; a decline's
note reaches the member, and fixing their profile puts them back in the queue.
"""
from __future__ import annotations

import datetime

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from accounts.models import MemberDocument, Membership, Role, User
from audit.models import AuditLog
from communications.models import Notification
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


@pytest.fixture
def secretary(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@imole.coop",
                                        full_name="Sec Retary", password="x")
        Membership.objects.create(
            user=user, member_no="SEC-1",
            role=Role.objects.filter(slug=Role.SECRETARY).first())
    return user


def _complete_profile(m):
    m.user.full_name = "Ada Obi"
    m.user.phone = "08030000000"
    m.user.save()
    m.photo = SimpleUploadedFile("p.jpg", b"x", content_type="image/jpeg")
    m.date_of_birth = datetime.date(1990, 1, 1)
    m.gender = "female"
    m.address = "1 Market Road"
    m.occupation = "Trader"
    m.next_of_kin_name = "Chi Obi"
    m.next_of_kin_phone = "08031111111"
    m.save()


def _add_id(m):
    with use_tenant(m.cooperative):
        return MemberDocument.objects.create(
            membership=m, doc_type=MemberDocument.DocType.ID,
            file=SimpleUploadedFile("id.pdf", b"x"))


def _fetch(client, m):
    return client.get(f"/api/v1/members/{m.id}/").json()


def test_a_bare_profile_is_incomplete_and_says_what_is_missing(
        coop, member, secretary):
    data = _fetch(_client(secretary, coop), member)

    assert data["verification_status"] == "incomplete"
    assert data["is_verified"] is False
    assert "Photo" in data["verification_missing"]
    assert "Means of ID document" in data["verification_missing"]
    assert [s["key"] for s in data["profile_steps"]] == [
        "details", "contact", "photo", "id"]
    assert 0 < data["profile_percent"] < 100


def test_a_complete_profile_without_id_is_not_ready(coop, member, secretary):
    _complete_profile(member)

    data = _fetch(_client(secretary, coop), member)

    assert data["verification_status"] == "incomplete"
    assert data["verification_missing"] == ["Means of ID document"]
    assert data["profile_percent"] == 90


def test_an_id_filed_under_the_wrong_type_does_not_count(coop, member,
                                                         secretary):
    """The trap behind the original report: an ID uploaded as "Other"."""
    _complete_profile(member)
    with use_tenant(coop):
        MemberDocument.objects.create(
            membership=member, doc_type=MemberDocument.DocType.OTHER,
            file=SimpleUploadedFile("id.pdf", b"x"))

    data = _fetch(_client(secretary, coop), member)
    assert data["verification_missing"] == ["Means of ID document"]


def test_a_passport_photograph_satisfies_the_photo_step(coop, member,
                                                        secretary):
    with use_tenant(coop):
        MemberDocument.objects.create(
            membership=member, doc_type=MemberDocument.DocType.PASSPORT,
            file=SimpleUploadedFile("p.jpg", b"x"))

    data = _fetch(_client(secretary, coop), member)
    assert "Photo" not in data["verification_missing"]


def test_completing_the_profile_awaits_review_not_verification(
        coop, member, secretary, django_capture_on_commit_callbacks):
    _complete_profile(member)
    client = _client(member.user, coop)

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            "/api/v1/me/documents/",
            {"doc_type": "id", "file": SimpleUploadedFile("id.pdf", b"x")},
            format="multipart")
    assert resp.status_code == 201, resp.content

    data = client.get("/api/v1/me/profile/").json()
    assert data["verification_status"] == "awaiting"
    assert data["profile_percent"] == 100
    assert data["is_verified"] is False
    # The member is told, and the officers are emailed.
    assert Notification.all_objects.filter(
        membership=member, kind=Notification.Kind.VERIFICATION).exists()
    assert any("Ready to verify" in m.subject for m in mail.outbox)
    # Listed with no query per row.
    listed = {m["id"]: m for m in _client(secretary, coop)
              .get("/api/v1/members/").json()["results"]}
    assert listed[member.id]["verification_status"] == "awaiting"


def test_an_officer_verifies(coop, member, secretary,
                             django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _client(secretary, coop).post(
            f"/api/v1/members/{member.id}/verify/")

    assert resp.status_code == 200, resp.content
    assert resp.json()["is_verified"] is True
    assert resp.json()["verification_status"] == "verified"
    assert resp.json()["verified_by_name"] == "Sec Retary"
    assert AuditLog.all_objects.filter(action="member.verified").exists()
    assert Notification.all_objects.filter(
        membership=member, title="You're verified").exists()


def test_a_decline_tells_the_member_why_and_a_fix_requeues_them(
        coop, member, secretary, django_capture_on_commit_callbacks):
    _complete_profile(member)
    _add_id(member)

    with django_capture_on_commit_callbacks(execute=True):
        resp = _client(secretary, coop).post(
            f"/api/v1/members/{member.id}/unverify/",
            {"note": "ID photo is blurry"}, format="json")
    assert resp.json()["verification_status"] == "withdrawn"
    assert resp.json()["verification_note"] == "ID photo is blurry"
    assert "ID photo is blurry" in Notification.all_objects.filter(
        membership=member).latest("created_at").body

    # The member uploads a clearer ID: back in the queue.
    client = _client(member.user, coop)
    client.post("/api/v1/me/documents/",
                {"doc_type": "id", "file": SimpleUploadedFile("id2.pdf", b"x")},
                format="multipart")
    assert client.get("/api/v1/me/profile/").json()[
        "verification_status"] == "awaiting"


def test_a_member_cannot_verify_themselves(coop, member):
    client = _client(member.user, coop)

    assert client.post(
        f"/api/v1/members/{member.id}/verify/").status_code == 403
    client.patch("/api/v1/me/profile/", {"verification": "officer"},
                 format="json")
    member.refresh_from_db()
    assert member.verification == ""
