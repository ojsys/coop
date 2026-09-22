"""
Password reset and the welcome invitation.

The security-relevant properties are pinned here: the endpoint must not reveal
which addresses are registered, links must expire on use, and a weak password
must be refused by Django's validators rather than silently accepted.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from accounts.models import User

pytestmark = pytest.mark.django_db

REQUEST_URL = "/api/v1/auth/password-reset/"
CONFIRM_URL = "/api/v1/auth/password-reset/confirm/"
GOOD_PASSWORD = "korede-ledger-2026"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """The reset endpoint is rate limited; stop one test throttling the next."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def person(db):
    return User.objects.create_user(
        email="adebayo@example.com", full_name="Adebayo Okonkwo",
        password="old-password-123",
    )


def _tokens_for(user):
    return (urlsafe_base64_encode(force_bytes(user.pk)),
            default_token_generator.make_token(user))


# ── Requesting a link ───────────────────────────────────────────────────────
def test_request_sends_a_branded_link(person):
    resp = APIClient().post(REQUEST_URL, {"email": person.email}, format="json")

    assert resp.status_code == 200
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [person.email]
    assert "/reset-password?uid=" in message.body
    assert "token=" in message.body
    # An HTML alternative must accompany the text part.
    html = message.alternatives[0][0]
    assert "<html" in html.lower()
    assert "Choose a new password" in html


def test_request_does_not_reveal_whether_an_address_exists(person):
    known = APIClient().post(REQUEST_URL, {"email": person.email},
                             format="json")
    cache.clear()
    unknown = APIClient().post(REQUEST_URL, {"email": "nobody@example.com"},
                               format="json")

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json(), "responses must be indistinguishable"
    assert len(mail.outbox) == 1, "no mail should go to an unknown address"


def test_request_is_rate_limited(person):
    client = APIClient()
    statuses = [
        client.post(REQUEST_URL, {"email": person.email}, format="json").status_code
        for _ in range(7)
    ]
    assert 429 in statuses, "an unauthenticated mail trigger must be throttled"


# ── Confirming ──────────────────────────────────────────────────────────────
def test_confirm_sets_the_new_password(person):
    uid, token = _tokens_for(person)

    resp = APIClient().post(CONFIRM_URL, {
        "uid": uid, "token": token, "password": GOOD_PASSWORD,
    }, format="json")

    assert resp.status_code == 200, resp.data
    person.refresh_from_db()
    assert person.check_password(GOOD_PASSWORD)


def test_a_link_cannot_be_used_twice(person):
    uid, token = _tokens_for(person)
    payload = {"uid": uid, "token": token, "password": GOOD_PASSWORD}

    assert APIClient().post(CONFIRM_URL, payload, format="json").status_code == 200
    # The token hashes the existing password, so changing it invalidates the link.
    again = APIClient().post(CONFIRM_URL, payload, format="json")
    assert again.status_code == 400


def test_a_tampered_token_is_refused(person):
    uid, token = _tokens_for(person)

    resp = APIClient().post(CONFIRM_URL, {
        "uid": uid, "token": token[:-1] + ("x" if token[-1] != "x" else "y"),
        "password": GOOD_PASSWORD,
    }, format="json")

    assert resp.status_code == 400
    person.refresh_from_db()
    assert not person.check_password(GOOD_PASSWORD)


def test_a_weak_password_is_refused(person):
    uid, token = _tokens_for(person)

    resp = APIClient().post(CONFIRM_URL, {
        "uid": uid, "token": token, "password": "12345678",
    }, format="json")

    assert resp.status_code == 400
    assert "password" in resp.data
    person.refresh_from_db()
    assert not person.check_password("12345678")


def test_a_garbage_uid_is_refused(person):
    resp = APIClient().post(CONFIRM_URL, {
        "uid": "!!!not-base64!!!", "token": "whatever",
        "password": GOOD_PASSWORD,
    }, format="json")
    assert resp.status_code == 400


# ── Welcome invitation ──────────────────────────────────────────────────────
def test_welcome_email_invites_the_member_to_set_a_password(coop, member):
    from communications.email import send_welcome_email

    mail.outbox.clear()
    assert send_welcome_email(member) is True

    message = mail.outbox[0]
    assert message.to == [member.user.email]
    assert coop.name in message.subject
    # A set-password link, never a generated password in plaintext.
    assert "/reset-password?uid=" in message.body
    assert member.member_no in message.body


def test_emails_are_branded_with_the_society(coop, member):
    from communications.email import send_welcome_email

    mail.outbox.clear()
    send_welcome_email(member)

    html = mail.outbox[0].alternatives[0][0]
    assert coop.name in html, "the society's own brand should head the email"
