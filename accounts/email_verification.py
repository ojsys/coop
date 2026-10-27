"""
Proving a person controls the email address on their account.

A signed token rather than Django's ``default_token_generator``: that one is
keyed on ``last_login``, so it is spent the moment the person signs in — and a
cooperative's founder is signed in by signup itself, before they ever open the
email. The token carries the address it was minted for, so a link sent before
an email change cannot confirm the new address.
"""
from __future__ import annotations

from django.core import signing
from django.utils import timezone

SALT = "accounts.email_verification"
MAX_AGE = 7 * 24 * 3600


def make_token(user) -> str:
    return signing.dumps({"u": user.pk, "e": user.email.lower()}, salt=SALT)


def user_for_token(token: str):
    """The user the token was minted for, or None if it is invalid, expired,
    or for an address the account no longer has."""
    from accounts.models import User

    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.BadSignature:
        return None
    user = User.objects.filter(pk=data.get("u")).first()
    if user is None or user.email.lower() != data.get("e"):
        return None
    return user


def mark_verified(user) -> None:
    if user.email_verified_at is None:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at", "updated_at"])
