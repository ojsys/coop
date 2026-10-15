"""The console's member list: complete, searchable, and private.

MembersPage searches, filters and counts on the client over whatever
``/members/`` hands back, so it must be able to read every member — in pages of
up to 500 — without that costing a query per member. Everyone's personal data
is for office-holders; an ordinary member reads only their own record.
"""
from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import MemberDocument, Membership, Role, User
from audit.models import AuditLog
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _client(user, coop=None):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    if coop is not None:
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}",
                           HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


def _person(coop, email, member_no, slug="member", name=None):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=name or email,
                                        password="pw")
        return Membership.objects.create(
            user=user, member_no=member_no,
            role=Role.objects.filter(slug=slug).first())


@pytest.fixture
def society(coop):
    for i in range(30):
        _person(coop, f"member{i}@imole.coop", f"IMC-{i:04d}",
                name=f"Member {i}")
    return coop


def test_an_officer_reads_every_member_in_one_page(society):
    officer = _person(society, "sec@imole.coop", "SEC-1", slug=Role.SECRETARY)
    with use_tenant(society):
        expected = Membership.objects.count()

    resp = _client(officer.user).get("/api/v1/members/?page_size=500")

    assert resp.status_code == 200, resp.content
    assert len(resp.json()["results"]) == expected


def test_listing_members_does_not_cost_a_query_per_member(society):
    officer = _person(society, "sec@imole.coop", "SEC-1", slug=Role.SECRETARY)
    client = _client(officer.user)
    with CaptureQueriesContext(connection) as queries:
        client.get("/api/v1/members/?page_size=500")
    assert len(queries) < 20, f"{len(queries)} queries for one page of members"


def test_listed_balance_matches_the_ledger(society):
    officer = _person(society, "sec@imole.coop", "SEC-1", slug=Role.SECRETARY)
    rows = _client(officer.user).get("/api/v1/members/?page_size=500").json()["results"]
    with use_tenant(society):
        for row in rows:
            m = Membership.objects.get(id=row["id"])
            from ledger.services import member_balance
            assert float(row["savings_balance"]) == float(member_balance(m))


def test_officers_search_by_name_number_email_or_phone(society):
    officer = _person(society, "sec@imole.coop", "SEC-1", slug=Role.SECRETARY)
    client = _client(officer.user)

    by_name = client.get("/api/v1/members/?search=member 1").json()["results"]
    assert {r["full_name"] for r in by_name} >= {"Member 1", "Member 12"}
    assert all("member 1" in r["full_name"].lower() for r in by_name)

    by_number = client.get("/api/v1/members/?search=IMC-0007").json()["results"]
    assert [r["member_no"] for r in by_number] == ["IMC-0007"]


def test_an_ordinary_member_sees_only_their_own_record(society):
    member = Membership.all_objects.get(member_no="IMC-0003")
    rows = _client(member.user).get("/api/v1/members/").json()["results"]
    assert [r["member_no"] for r in rows] == ["IMC-0003"]

    other = Membership.all_objects.get(member_no="IMC-0004")
    assert _client(member.user).get(
        f"/api/v1/members/{other.id}/").status_code == 404


def test_a_member_cannot_see_or_delete_anyone_elses_documents(society):
    owner = Membership.all_objects.get(member_no="IMC-0001")
    nosy = Membership.all_objects.get(member_no="IMC-0002")
    with use_tenant(society):
        doc = MemberDocument.objects.create(
            membership=owner, doc_type="id",
            file=SimpleUploadedFile("nin.png", b"x", content_type="image/png"))

    client = _client(nosy.user)
    rows = client.get("/api/v1/member-documents/").json()["results"]
    assert rows == []
    assert client.delete(f"/api/v1/member-documents/{doc.id}/").status_code in (403, 404)
    assert MemberDocument.all_objects.filter(id=doc.id).exists()


def test_platform_staff_viewing_a_profile_is_audited(society):
    staff = User.objects.create_user(email="ops@platform.ng", full_name="Ops",
                                     password="pw", is_platform_admin=True)
    member = Membership.all_objects.get(member_no="IMC-0005")
    client = _client(staff, society)

    found = client.get("/api/v1/members/?search=IMC-0005").json()["results"]
    assert [r["id"] for r in found] == [member.id]
    resp = client.get(f"/api/v1/members/{member.id}/")

    assert resp.status_code == 200, resp.content
    assert "address" in resp.json() and "date_of_birth" in resp.json()
    assert AuditLog.all_objects.filter(
        action="member.profile_viewed_by_platform",
        entity_id=str(member.id)).exists()


def test_platform_staff_find_a_person_across_societies(society, other_coop):
    staff = User.objects.create_user(email="ops@platform.ng", full_name="Ops",
                                     password="pw", is_platform_admin=True)
    _person(other_coop, "ada@aba.coop", "ABA-1", name="Ada Obi")

    rows = _client(staff).get("/api/v1/platform/member-search/?q=ada").json()
    assert [(r["full_name"], r["cooperative_id"]) for r in rows] == [
        ("Ada Obi", other_coop.id)]

    member = Membership.all_objects.get(member_no="IMC-0001")
    assert _client(member.user).get(
        "/api/v1/platform/member-search/?q=ada").status_code == 403
