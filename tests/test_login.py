"""
Signing in.

Login is the only endpoint that accepts a guessed password, so the properties
pinned here are: it still works, a refusal answers 400 (which is what the
sign-in form branches on to decide between "wrong password" and "the server is
down"), the refusal does not say whether the address is registered, and
repeated attempts are throttled — DRF's stock ``obtain_auth_token``, which this
replaced, has no rate limit at all.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import User

pytestmark = pytest.mark.django_db

LOGIN_URL = "/api/v1/auth/token/"
PASSWORD = "korede-ledger-2026"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """Login is rate limited; stop one test throttling the next."""
    cache.clear()


@pytest.fixture
def person(db):
    return User.objects.create_user(
        email="adebayo@example.com", full_name="Adebayo Okonkwo",
        password=PASSWORD,
    )


def test_login_returns_a_token(person):
    resp = APIClient().post(
        LOGIN_URL, {"username": person.email, "password": PASSWORD},
        format="json",
    )

    assert resp.status_code == 200, resp.content
    assert resp.json()["token"]


def test_wrong_password_is_refused_with_400(person):
    """The sign-in form reads 400/401 as "invalid credentials" and anything
    else as an outage. If this status ever moves, that branch moves with it."""
    resp = APIClient().post(
        LOGIN_URL, {"username": person.email, "password": "not-the-password"},
        format="json",
    )

    assert resp.status_code == 400, resp.content


def test_refusal_does_not_reveal_whether_the_address_exists(person):
    known = APIClient().post(
        LOGIN_URL, {"username": person.email, "password": "wrong"},
        format="json",
    )
    cache.clear()
    unknown = APIClient().post(
        LOGIN_URL, {"username": "nobody@example.com", "password": "wrong"},
        format="json",
    )

    assert known.status_code == unknown.status_code
    assert known.json() == unknown.json(), "replies must be indistinguishable"


def test_login_is_rate_limited(person):
    """Without this the one endpoint that accepts guessed passwords was the
    only unthrottled one — password reset and signup were both limited."""
    client = APIClient()
    statuses = [
        client.post(
            LOGIN_URL, {"username": person.email, "password": "wrong"},
            format="json",
        ).status_code
        for _ in range(35)  # the scope allows 30/min
    ]

    assert 429 in statuses, "the credential endpoint must be throttled"
