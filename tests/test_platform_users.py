"""
Account recovery: a person changing their own password, and platform staff
fixing an account for someone who cannot.

Pinned here: changing your own password needs the current one and rotates the
token; only platform admins reach the staff screen; a superuser's account is
out of bounds to ordinary staff; every staff change lands in the audit log,
and no password ever does.
"""
from __future__ import annotations

import json

import pytest
from django.core import mail
from django.core.cache import cache
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import User
from audit.models import AuditLog

pytestmark = pytest.mark.django_db

OLD = "korede-ledger-2026"
NEW = "amaka-passbook-2027"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()


@pytest.fixture
def person(db):
    return User.objects.create_user(
        email="chidi@example.com", full_name="Chidi Okafor", password=OLD,
        phone="08030000000",
    )


@pytest.fixture
def staff(db):
    return User.objects.create_user(
        email="ops@example.com", full_name="Ops Desk", password="pw",
        is_platform_admin=True,
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


# --- Changing your own password -------------------------------------------

def test_change_own_password_rotates_the_token(person):
    old_token = Token.objects.create(user=person)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {old_token.key}")

    resp = client.post("/api/v1/me/password/",
                       {"current_password": OLD, "new_password": NEW},
                       format="json")

    assert resp.status_code == 200, resp.content
    person.refresh_from_db()
    assert person.check_password(NEW)
    new_key = resp.json()["token"]
    assert new_key != old_token.key
    assert not Token.objects.filter(key=old_token.key).exists()
    assert AuditLog.all_objects.filter(
        action="account.password_changed", entity_id=str(person.pk)).exists()


def test_change_own_password_needs_the_current_one(person):
    resp = client_for(person).post(
        "/api/v1/me/password/",
        {"current_password": "guess", "new_password": NEW}, format="json")

    assert resp.status_code == 400
    assert "current_password" in resp.json()
    person.refresh_from_db()
    assert person.check_password(OLD)


def test_change_own_password_applies_the_password_rules(person):
    resp = client_for(person).post(
        "/api/v1/me/password/",
        {"current_password": OLD, "new_password": "12345678"}, format="json")

    assert resp.status_code == 400
    assert "new_password" in resp.json()


def test_change_password_requires_sign_in():
    resp = APIClient().post("/api/v1/me/password/",
                            {"current_password": OLD, "new_password": NEW},
                            format="json")
    assert resp.status_code in (401, 403)


# --- Platform staff managing accounts --------------------------------------

def test_only_platform_admins_can_list_users(person, staff):
    assert client_for(person).get(
        "/api/v1/platform-users/").status_code == 403
    resp = client_for(staff).get("/api/v1/platform-users/?search=chidi")
    assert resp.status_code == 200
    assert [r["email"] for r in resp.json()["results"]] == [person.email]


def test_staff_rename_is_audited_with_before_and_after(person, staff):
    resp = client_for(staff).patch(
        f"/api/v1/platform-users/{person.pk}/",
        {"full_name": "Chidinma Okafor"}, format="json")

    assert resp.status_code == 200, resp.content
    person.refresh_from_db()
    assert person.full_name == "Chidinma Okafor"
    entry = AuditLog.all_objects.get(action="platform.user_updated")
    assert entry.actor == staff
    assert entry.before == {"full_name": "Chidi Okafor"}
    assert entry.after == {"full_name": "Chidinma Okafor"}


def test_staff_cannot_grant_platform_admin(person, staff):
    client_for(staff).patch(f"/api/v1/platform-users/{person.pk}/",
                            {"is_platform_admin": True}, format="json")
    person.refresh_from_db()
    assert not person.is_platform_admin


def test_email_must_stay_unique(person, staff):
    resp = client_for(staff).patch(
        f"/api/v1/platform-users/{person.pk}/",
        {"email": "OPS@example.com"}, format="json")

    assert resp.status_code == 400
    assert "email" in resp.json()


def test_staff_set_password_signs_the_person_out_and_logs_no_secret(
        person, staff):
    Token.objects.create(user=person)

    resp = client_for(staff).post(
        f"/api/v1/platform-users/{person.pk}/set-password/",
        {"password": NEW}, format="json")

    assert resp.status_code == 200, resp.content
    person.refresh_from_db()
    assert person.check_password(NEW)
    assert not Token.objects.filter(user=person).exists()
    entry = AuditLog.all_objects.get(action="platform.user_password_set")
    assert entry.actor == staff
    assert NEW not in json.dumps([entry.before, entry.after])


def test_staff_can_email_a_reset_link(person, staff):
    resp = client_for(staff).post(
        f"/api/v1/platform-users/{person.pk}/send-reset-link/")

    assert resp.status_code == 200, resp.content
    assert [m.to for m in mail.outbox] == [[person.email]]
    assert AuditLog.all_objects.filter(
        action="platform.user_reset_link_sent").exists()


def test_ordinary_staff_cannot_touch_a_superuser(staff):
    root = User.objects.create_superuser(
        email="root@example.com", full_name="Root", password=OLD)

    client = client_for(staff)
    assert client.patch(f"/api/v1/platform-users/{root.pk}/",
                        {"full_name": "X"}, format="json").status_code == 403
    assert client.post(f"/api/v1/platform-users/{root.pk}/set-password/",
                       {"password": NEW}, format="json").status_code == 403
    root.refresh_from_db()
    assert root.full_name == "Root" and root.check_password(OLD)


def test_activity_lists_staff_changes_and_one_persons_history(person, staff):
    client = client_for(staff)
    client.patch(f"/api/v1/platform-users/{person.pk}/",
                 {"phone": "08031111111"}, format="json")
    client_for(person).post(
        "/api/v1/me/password/",
        {"current_password": OLD, "new_password": NEW}, format="json")

    everyone = client.get("/api/v1/platform-users/activity/").json()["results"]
    assert [e["action"] for e in everyone] == ["platform.user_updated"]
    assert everyone[0]["actor_name"] == "Ops Desk"

    mine = client.get(
        f"/api/v1/platform-users/activity/?user={person.pk}").json()["results"]
    assert {e["action"] for e in mine} == {
        "platform.user_updated", "account.password_changed"}
