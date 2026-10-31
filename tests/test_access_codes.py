"""
Access codes, and choosing a password when asking to join.

How a member with no email gets into their account: an officer issues a
one-time code from their record, and the member uses it with their phone to
choose a password. Or they chose one when they applied, and it works as soon
as they are admitted.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.access_codes import MAX_ATTEMPTS, AccessCode
from accounts.models import JoinRequest, Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db

REDEEM_URL = "/api/v1/auth/access-code/"
NEW_PASSWORD = "maize-harvest-2026"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()


def _officer(coop, slug, email):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=slug.title(),
                                        password="x")
        return Membership.objects.create(
            user=user, member_no=f"IMC-{slug[:3]}",
            role=Role.objects.get(slug=slug))


@pytest.fixture
def secretary(coop):
    return _officer(coop, "secretary", "sec@imole.test")


@pytest.fixture
def chair(coop):
    return _officer(coop, "chairperson", "chair@imole.test")


@pytest.fixture
def farmer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(phone="08034412290",
                                        full_name="Musa Bello")
        user.set_unusable_password()
        user.save()
        return Membership.objects.create(user=user, member_no="IMC-0100")


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


def _issue(officer, member, coop):
    return _client(officer.user, coop).post(
        f"/api/v1/members/{member.pk}/access-code/", format="json")


def _redeem(code, username="0803 441 2290", password=NEW_PASSWORD):
    return APIClient().post(REDEEM_URL, {"username": username, "code": code,
                                         "password": password}, format="json")


def _login(username, password=NEW_PASSWORD):
    return APIClient().post("/api/v1/auth/token/", {
        "username": username, "password": password}, format="json")


# ── Issuing ─────────────────────────────────────────────────────────────────
def test_an_officer_issues_a_code_shown_once(coop, secretary, farmer):
    resp = _issue(secretary, farmer, coop)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert len(body["code"]) == 8 and body["code"].isdigit()
    assert body["sign_in_with"] == "+2348034412290"
    row = AccessCode.objects.get(user=farmer.user)
    assert body["code"] not in row.code_hash
    assert row.issued_by == secretary.user


def test_issuing_is_audited(coop, secretary, farmer):
    from audit.models import AuditLog

    _issue(secretary, farmer, coop)
    assert AuditLog.all_objects.filter(
        action="member.access_code_issued").exists()


def test_an_ordinary_member_cannot_issue_codes(coop, farmer):
    with use_tenant(coop):
        other = Membership.objects.create(
            user=User.objects.create_user(phone="08055501234",
                                          full_name="Ada"),
            member_no="IMC-0101")
    resp = _issue(farmer, other, coop)
    assert resp.status_code == 403


def test_an_officer_cannot_issue_for_a_member_of_another_society(
        coop, other_coop, secretary):
    with use_tenant(other_coop):
        outsider = Membership.objects.create(
            user=User.objects.create_user(phone="08055501234",
                                          full_name="Ada"),
            member_no="X-1")
    assert _issue(secretary, outsider, coop).status_code == 404


def test_only_the_chairperson_issues_one_for_another_officer(
        coop, secretary, chair):
    treasurer = _officer(coop, "treasurer", "tre@imole.test")
    refused = _issue(secretary, treasurer, coop)
    assert refused.status_code == 400
    assert "Chairperson" in refused.json()["detail"]
    assert _issue(chair, treasurer, coop).status_code == 200


def test_no_code_for_an_officer_of_another_society(coop, other_coop,
                                                   secretary, farmer):
    with use_tenant(other_coop):
        Membership.objects.create(user=farmer.user, member_no="X-1",
                                  role=Role.objects.get(slug="treasurer"))
    resp = _issue(secretary, farmer, coop)
    assert resp.status_code == 400
    assert "another society" in resp.json()["detail"]


def test_an_ordinary_member_of_two_societies_can_get_a_code(
        coop, other_coop, secretary, farmer):
    with use_tenant(other_coop):
        Membership.objects.create(user=farmer.user, member_no="X-1")
    assert _issue(secretary, farmer, coop).status_code == 200


def test_no_code_for_platform_staff(coop, secretary, farmer):
    farmer.user.is_platform_admin = True
    farmer.user.save()
    assert _issue(secretary, farmer, coop).status_code == 400


def test_no_code_for_yourself(coop, secretary):
    assert _issue(secretary, secretary, coop).status_code == 400


def test_someone_with_an_email_is_told_a_code_was_issued(
        coop, secretary, django_capture_on_commit_callbacks):
    with use_tenant(coop):
        member = Membership.objects.create(
            user=User.objects.create_user(email="ada@example.com",
                                          full_name="Ada"),
            member_no="IMC-0102")
    with django_capture_on_commit_callbacks(execute=True):
        code = _issue(secretary, member, coop).json()["code"]
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ada@example.com"]
    assert code not in mail.outbox[0].body


# ── Using a code ────────────────────────────────────────────────────────────
def test_the_member_sets_a_password_and_signs_in_by_phone(coop, secretary,
                                                          farmer):
    code = _issue(secretary, farmer, coop).json()["code"]
    resp = _redeem(code)
    assert resp.status_code == 200, resp.content
    assert _login("08034412290").status_code == 200


def test_the_code_can_be_typed_with_spaces(coop, secretary, farmer):
    code = _issue(secretary, farmer, coop).json()["code"]
    assert _redeem(f"{code[:4]} {code[4:]}").status_code == 200


def test_a_code_works_once(coop, secretary, farmer):
    code = _issue(secretary, farmer, coop).json()["code"]
    assert _redeem(code).status_code == 200
    assert _redeem(code, password="another-pass-2026").status_code == 400


def test_a_new_code_cancels_the_old_one(coop, secretary, farmer):
    first = _issue(secretary, farmer, coop).json()["code"]
    second = _issue(secretary, farmer, coop).json()["code"]
    if first != second:
        assert _redeem(first).status_code == 400
    assert _redeem(second).status_code == 200


def test_an_expired_code_does_not_work(coop, secretary, farmer):
    code = _issue(secretary, farmer, coop).json()["code"]
    AccessCode.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    assert _redeem(code).status_code == 400


def test_a_code_is_burnt_after_too_many_wrong_tries(coop, secretary, farmer):
    code = _issue(secretary, farmer, coop).json()["code"]
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(MAX_ATTEMPTS):
        assert _redeem(wrong).status_code == 400
    assert _redeem(code).status_code == 400


def test_a_code_only_works_for_its_own_person(coop, secretary, farmer):
    with use_tenant(coop):
        Membership.objects.create(
            user=User.objects.create_user(phone="08055501234",
                                          full_name="Ada"),
            member_no="IMC-0103")
    code = _issue(secretary, farmer, coop).json()["code"]
    assert _redeem(code, username="08055501234").status_code == 400


def test_an_unknown_phone_gets_the_same_answer_as_a_wrong_code(
        coop, secretary, farmer):
    _issue(secretary, farmer, coop)
    wrong_code = _redeem("12345678")
    unknown = _redeem("12345678", username="08099999999")
    assert wrong_code.status_code == unknown.status_code == 400
    assert wrong_code.json() == unknown.json()


def test_a_weak_password_is_refused_and_the_code_survives(coop, secretary,
                                                          farmer):
    code = _issue(secretary, farmer, coop).json()["code"]
    assert _redeem(code, password="123").status_code == 400
    assert _redeem(code).status_code == 200


def test_using_a_code_signs_out_old_sessions(coop, secretary, farmer):
    Token.objects.create(user=farmer.user)
    code = _issue(secretary, farmer, coop).json()["code"]
    _redeem(code)
    assert not Token.objects.filter(user=farmer.user).exists()


# ── Forgot password ─────────────────────────────────────────────────────────
def test_forgot_password_takes_a_phone_and_sends_nothing_without_email(
        farmer):
    resp = APIClient().post("/api/v1/auth/password-reset/",
                            {"username": "08034412290"}, format="json")
    assert resp.status_code == 200
    assert "access code" in resp.json()["detail"]
    assert mail.outbox == []


def test_forgot_password_by_phone_emails_someone_who_has_an_email():
    User.objects.create_user(phone="08034412290", email="ada@example.com",
                             full_name="Ada", password="x")
    APIClient().post("/api/v1/auth/password-reset/",
                     {"username": "0803 441 2290"}, format="json")
    assert [m.to for m in mail.outbox] == [["ada@example.com"]]


def test_forgot_password_still_takes_email_from_old_clients():
    User.objects.create_user(email="ada@example.com", full_name="Ada",
                             password="x")
    APIClient().post("/api/v1/auth/password-reset/",
                     {"email": "ada@example.com"}, format="json")
    assert len(mail.outbox) == 1


# ── Choosing a password when asking to join ─────────────────────────────────
def _apply(coop, **extra):
    return APIClient().post("/api/v1/public/join/", {
        "cooperative": coop.id, "full_name": "Musa Bello",
        "phone": "0803 441 2290", **extra}, format="json")


def test_the_chosen_password_works_once_admitted(coop, secretary):
    assert _apply(coop, password=NEW_PASSWORD).status_code == 201
    request = JoinRequest.all_objects.get()
    assert request.password_hash and NEW_PASSWORD not in request.password_hash
    assert _login("08034412290").status_code == 400  # no account yet

    resp = _client(secretary.user, coop).post(
        f"/api/v1/join-requests/{request.pk}/approve/", {}, format="json")
    assert resp.status_code == 200, resp.content
    assert "password they chose" in resp.json()["detail"]
    assert resp.json()["needs_access_code"] is False
    assert _login("08034412290").status_code == 200
    request.refresh_from_db()
    assert request.password_hash == ""


def test_a_weak_password_is_refused_when_applying(coop):
    resp = _apply(coop, password="123")
    assert resp.status_code == 400
    assert "password" in resp.json()


def test_applying_without_a_password_still_works(coop):
    """App builds that predate the field send none."""
    assert _apply(coop).status_code == 201
    assert JoinRequest.all_objects.get().password_hash == ""


def test_a_chosen_password_never_replaces_an_existing_accounts(coop,
                                                               secretary):
    User.objects.create_user(phone="08034412290", full_name="Musa",
                             password="original-pass-2026")
    _apply(coop, password=NEW_PASSWORD)
    request = JoinRequest.all_objects.get()
    resp = _client(secretary.user, coop).post(
        f"/api/v1/join-requests/{request.pk}/approve/", {}, format="json")
    assert resp.status_code == 200
    assert _login("08034412290").status_code == 400
    assert _login("08034412290", "original-pass-2026").status_code == 200


def test_declining_a_request_discards_the_chosen_password(coop, secretary):
    _apply(coop, password=NEW_PASSWORD)
    request = JoinRequest.all_objects.get()
    _client(secretary.user, coop).post(
        f"/api/v1/join-requests/{request.pk}/reject/", {}, format="json")
    request.refresh_from_db()
    assert request.password_hash == ""


def test_admitting_someone_with_no_email_or_password_asks_for_a_code(
        coop, secretary):
    _apply(coop)
    request = JoinRequest.all_objects.get()
    resp = _client(secretary.user, coop).post(
        f"/api/v1/join-requests/{request.pk}/approve/", {}, format="json")
    assert resp.json()["needs_access_code"] is True
    assert "access code" in resp.json()["detail"]
