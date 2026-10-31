"""
Member numbers are assigned by the platform, per cooperative.

Never typed by an officer or chosen by a member: the next number follows the
highest issued, so a former member's number is never reused.
"""
from __future__ import annotations

import io

import pytest
from rest_framework.test import APIClient

from accounts.join_services import approve_join_request, next_member_no
from accounts.models import JoinRequest, Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def officer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@imole.test",
                                        full_name="Sec", password="x")
        return Membership.objects.create(
            user=user, member_no="IMC-0005",
            role=Role.objects.get(slug="secretary"))


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


def _prefix(coop):
    return next_member_no(coop).split("-")[0]


def test_the_next_number_follows_the_highest_never_a_gap(coop, officer):
    prefix = _prefix(coop)
    with use_tenant(coop):
        for n in (1, 7):
            Membership.objects.create(
                user=User.objects.create_user(email=f"m{n}@x.test",
                                              full_name="M"),
                member_no=f"{prefix}-{n:04d}")
    assert next_member_no(coop) == f"{prefix}-0008"


def test_numbering_is_per_cooperative(coop, other_coop, officer):
    assert next_member_no(other_coop).endswith("-0001")


def test_adding_a_member_assigns_the_number(coop, officer):
    client = _client(officer.user, coop)
    resp = client.post("/api/v1/members/", {
        "member_no": "MINE-1", "full_name": "Ada Obi",
        "email": "ada@x.test", "kind": "member"}, format="json")

    assert resp.status_code == 201, resp.json()
    # The officer holds IMC-0005, so the next is 0006 — not what was sent.
    assert resp.json()["member_no"] == f"{_prefix(coop)}-0006"


def test_the_number_cannot_be_edited(coop, officer, member):
    client = _client(officer.user, coop)
    before = member.member_no
    client.patch(f"/api/v1/members/{member.id}/", {"member_no": "X-1"},
                 format="json")
    member.refresh_from_db()
    assert member.member_no == before


def test_admitting_a_join_request_assigns_the_number(coop, officer):
    request = JoinRequest.all_objects.create(
        cooperative=coop, full_name="Bola", email="bola@x.test")
    membership = approve_join_request(request, actor=officer.user)
    assert membership.member_no.startswith(_prefix(coop) + "-")
    assert membership.member_no != officer.member_no


def test_import_assigns_numbers_but_keeps_an_existing_register(coop, officer):
    csv = ("member_no,full_name,email\n"
           ",New Person,new@x.test\n"
           "OLD-0042,Long Standing,old@x.test\n")
    upload = io.BytesIO(csv.encode())
    upload.name = "members.csv"
    resp = _client(officer.user, coop).post(
        "/api/v1/members/import/", {"file": upload}, format="multipart")

    assert resp.json()["created"] == 2, resp.json()
    with use_tenant(coop):
        numbers = set(Membership.objects.values_list("member_no", flat=True))
    assert "OLD-0042" in numbers
    assert any(n.startswith(_prefix(coop) + "-") and n != officer.member_no
               for n in numbers)
