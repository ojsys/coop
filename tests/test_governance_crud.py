"""Phase 4: governance CRUD depth — meeting edit/delete, minutes, attendance."""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from governance.models import Attendance, Meeting

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


def test_meeting_crud_with_minutes(coop):
    client = _client(_admin(coop))
    r = client.post("/api/v1/meetings/", {
        "title": "AGM 2026", "location": "Lagos",
        "scheduled_at": "2026-09-01T10:00:00Z",
    }, format="json")
    assert r.status_code == 201, r.content
    mid = r.json()["id"]

    r = client.patch(f"/api/v1/meetings/{mid}/",
                     {"minutes": "Approved the budget.", "status": "concluded"},
                     format="json")
    assert r.status_code == 200
    assert r.json()["minutes"] == "Approved the budget."

    assert client.delete(f"/api/v1/meetings/{mid}/").status_code == 204
    with use_tenant(coop):
        assert not Meeting.objects.filter(id=mid).exists()


def test_attendance_record_and_remove(coop, member):
    client = _client(_admin(coop))
    with use_tenant(coop):
        meeting = Meeting.objects.create(title="M", scheduled_at="2026-09-01T10:00:00Z")

    r = client.post("/api/v1/attendances/",
                    {"meeting": meeting.id, "membership": member.id, "present": True},
                    format="json")
    assert r.status_code == 201, r.content
    aid = r.json()["id"]
    assert r.json()["member_no"] == member.member_no

    # Meeting attendance_count reflects it.
    assert client.get(f"/api/v1/meetings/{meeting.id}/").json()["attendance_count"] == 1

    assert client.delete(f"/api/v1/attendances/{aid}/").status_code == 204
    with use_tenant(coop):
        assert not Attendance.objects.filter(id=aid).exists()
