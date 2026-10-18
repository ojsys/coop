"""
A member of one society asking to join another, with the account they have.

No second account and nothing retyped: the request is filed from the signed-in
person's own details, officers decide it in their usual queue, and approving
adds a membership to the same account, so the society shows up in the
switcher. Someone who already has a password is told to switch, not sent a
set-password link.
"""
from __future__ import annotations

import pytest
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.join_models import JoinRequest
from accounts.join_services import approve_join_request
from accounts.models import Membership, Role, User
from core.context import use_tenant
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db
URL = "/api/v1/me/join-requests/"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def aba(other_coop):
    other_coop.status = Cooperative.Status.ACTIVE
    other_coop.save()
    return other_coop


@pytest.fixture
def signed_in(member):
    member.user.set_password("pw")
    member.user.save()
    client = APIClient()
    client.force_authenticate(member.user)
    return client


def test_request_by_id_uses_my_own_details(signed_in, member, aba):
    resp = signed_in.post(URL, {"cooperative": aba.id,
                                "message": "Introduced by Mrs Eze"},
                          format="json")

    assert resp.status_code == 201, resp.content
    assert resp.json()["cooperative_name"] == aba.name
    req = JoinRequest.all_objects.get(cooperative=aba)
    assert req.email == member.user.email
    assert req.full_name == member.user.full_name
    assert req.phone == member.user.phone
    assert [r["id"] for r in signed_in.get(URL).json()] == [req.id]


@pytest.mark.parametrize("code", ["aba", "https://mycooperativeos.com/join/aba",
                                  "mycooperativeos.com/join/ABA/"])
def test_an_invite_code_or_pasted_link_works(signed_in, aba, code):
    resp = signed_in.post(URL, {"code": code}, format="json")
    assert resp.status_code == 201, resp.content


def test_cannot_ask_a_society_you_already_belong_to(signed_in, coop):
    resp = signed_in.post(URL, {"cooperative": coop.id}, format="json")
    assert resp.status_code == 400
    assert "already belongs" in str(resp.json())


def test_a_switched_off_link_finds_nothing(signed_in, aba):
    aba.join_link_enabled = False
    aba.save()
    assert signed_in.post(URL, {"code": "aba"}, format="json").status_code == 400


def test_withdraw_a_waiting_request(signed_in, aba, make_member):
    req_id = signed_in.post(URL, {"cooperative": aba.id},
                            format="json").json()["id"]
    assert signed_in.delete(f"{URL}{req_id}/").status_code == 204
    assert not JoinRequest.all_objects.exists()
    # Nobody else's.
    other = JoinRequest.all_objects.create(
        cooperative=aba, full_name="X", email="x@example.com")
    assert signed_in.delete(f"{URL}{other.id}/").status_code == 404


def test_approval_adds_the_society_to_the_same_account(
        signed_in, member, aba, django_capture_on_commit_callbacks):
    signed_in.post(URL, {"cooperative": aba.id}, format="json")
    req = JoinRequest.all_objects.get(cooperative=aba)
    with use_tenant(aba):
        officer = User.objects.create_user(email="sec@aba.coop",
                                           full_name="Sec", password="x")
        Membership.objects.create(user=officer, member_no="S1",
                                  role=Role.objects.get(slug="secretary"))
    officers = APIClient()
    officers.force_authenticate(officer)
    officers.credentials(HTTP_X_COOPERATIVE_ID=str(aba.id))
    listed = officers.get("/api/v1/join-requests/").json()["results"]
    assert listed[0]["has_account"] is True

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        approve_join_request(req, actor=officer, member_no="ABA-1")

    me = signed_in.get("/api/v1/me/").json()
    assert {m["cooperative_name"] for m in me["memberships"]} == {
        member.cooperative.name, aba.name}
    assert User.objects.filter(email=member.user.email).count() == 1
    # Told to switch; no set-password link for a password they already have.
    welcome = mail.outbox[-1]
    body = welcome.alternatives[0][0] if welcome.alternatives else welcome.body
    assert "switch" in body and "reset-password" not in body
    # And the request no longer shows as waiting.
    assert signed_in.get(URL).json() == []
