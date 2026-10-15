"""
The verified badge.

A member earns it by completing their profile and uploading a means of ID, or
an officer grants it outright. Pinned here: the automatic route really does
follow the profile (both ways), an officer's decision overrides it in both
directions, only privileged officers can make that decision, and it is audited.
"""
from __future__ import annotations

import datetime

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from accounts.models import MemberDocument, Membership, Role, User
from audit.models import AuditLog
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


def test_a_bare_profile_is_not_verified_and_says_what_is_missing(
        coop, member, secretary):
    data = _fetch(_client(secretary, coop), member)

    assert data["is_verified"] is False
    assert "Photo" in data["verification_missing"]
    assert "Means of ID document" in data["verification_missing"]


def test_a_complete_profile_without_id_is_not_enough(coop, member, secretary):
    _complete_profile(member)

    data = _fetch(_client(secretary, coop), member)

    assert data["is_verified"] is False
    assert data["verification_missing"] == ["Means of ID document"]


def test_complete_profile_and_id_verifies_automatically(coop, member,
                                                        secretary):
    _complete_profile(member)
    doc = _add_id(member)
    client = _client(secretary, coop)

    data = _fetch(client, member)
    assert data["is_verified"] is True
    assert data["verified_via"] == "profile"
    # The list carries it too, without a query per row.
    listed = {m["id"]: m for m in client.get("/api/v1/members/").json()["results"]}
    assert listed[member.id]["is_verified"] is True

    # And it follows the profile back: remove the ID, lose the badge.
    doc.delete()
    assert _fetch(client, member)["is_verified"] is False


def test_an_officer_can_verify_a_member_with_a_bare_profile(coop, member,
                                                            secretary):
    client = _client(secretary, coop)

    resp = client.post(f"/api/v1/members/{member.id}/verify/")

    assert resp.status_code == 200, resp.content
    assert resp.json()["is_verified"] is True
    assert resp.json()["verified_via"] == "officer"
    assert resp.json()["verified_by_name"] == "Sec Retary"
    assert AuditLog.all_objects.filter(action="member.verified").exists()


def test_an_officer_withdrawal_overrides_a_complete_profile(coop, member,
                                                            secretary):
    _complete_profile(member)
    _add_id(member)
    client = _client(secretary, coop)

    resp = client.post(f"/api/v1/members/{member.id}/unverify/")

    assert resp.json()["is_verified"] is False
    assert resp.json()["verification_withdrawn"] is True
    assert AuditLog.all_objects.filter(
        action="member.verification_withdrawn").exists()


def test_a_member_cannot_verify_themselves(coop, member):
    client = _client(member.user, coop)

    assert client.post(
        f"/api/v1/members/{member.id}/verify/").status_code == 403
    # Nor sneak it in through their own profile.
    client.patch("/api/v1/me/profile/", {"verification": "officer"},
                 format="json")
    member.refresh_from_db()
    assert member.verification == ""


def test_a_member_sees_their_own_checklist(coop, member):
    data = _client(member.user, coop).get("/api/v1/me/profile/").json()

    assert data["is_verified"] is False
    assert "Means of ID document" in data["verification_missing"]
