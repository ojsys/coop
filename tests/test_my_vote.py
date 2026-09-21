"""my_vote field on resolutions — lets clients hide vote buttons after voting."""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from governance.models import Meeting, Resolution
from governance.services import open_resolution

pytestmark = pytest.mark.django_db


def _member(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="ada@x.co", full_name="Ada",
                                     password="x")
        m = Membership.objects.create(
            user=u, member_no="M-1",
            role=Role.objects.filter(slug="member").first())
        meeting = Meeting.objects.create(title="AGM",
                                         scheduled_at="2026-09-01T10:00:00Z")
        res = Resolution.objects.create(meeting=meeting, title="Motion",
                                        created_by=u)
        open_resolution(res, actor=u)
    return u, m, res


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def test_my_vote_null_before_and_set_after(coop):
    user, membership, res = _member(coop)
    client = _client(user)

    # Before voting: my_vote is null.
    row = next(r for r in client.get("/api/v1/resolutions/").json()["results"]
               if r["id"] == res.id)
    assert row["my_vote"] is None

    # Vote.
    assert client.post(f"/api/v1/resolutions/{res.id}/vote/",
                       {"choice": "for", "membership": membership.id},
                       format="json").status_code == 201

    # After voting: my_vote reflects the choice.
    row = next(r for r in client.get("/api/v1/resolutions/").json()["results"]
               if r["id"] == res.id)
    assert row["my_vote"] == "for"

    # A second vote is correctly rejected (one member, one vote).
    dup = client.post(f"/api/v1/resolutions/{res.id}/vote/",
                      {"choice": "against", "membership": membership.id},
                      format="json")
    assert dup.status_code == 400
