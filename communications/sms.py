"""
Text messages.

Only what has to reach someone who may have no email goes by SMS: access
codes (accounts.access_codes). Announcements still use the stub in
communications.services; bulk SMS costs money per message and Nigeria's DND
route carries transactional messages only.

The backend is chosen by ``settings.SMS_BACKEND``:

- ``"termii"``: Termii's HTTP API, the live provider.
- ``"console"``: printed instead of sent, for local development.
- ``"locmem"``: appended to :data:`outbox`, for tests.
- ``""``: switched off. :func:`sms_enabled` is false and nothing is sent, so
  members fall back to codes handed over by an officer.

Every attempt is logged as an :class:`~communications.models.SmsMessage`
without its text, which for an access code is the secret itself. The log is
also what enforces the daily limit, which caps the bill if someone tries to
run up messages to numbers they do not own.
"""
from __future__ import annotations

import logging
import sys
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# What the "locmem" backend has sent: dicts of {"to", "body"}.
outbox: list[dict] = []


class SmsError(Exception):
    """The provider did not accept the message."""


def sms_enabled() -> bool:
    return bool(getattr(settings, "SMS_BACKEND", ""))


def can_text(phone) -> bool:
    """Whether this E.164 number may be sent a text at all.

    Only to the countries the platform serves, so a stranger cannot have it
    pay for messages to premium numbers abroad.
    """
    if not (sms_enabled() and phone):
        return False
    codes = getattr(settings, "SMS_ALLOWED_COUNTRY_CODES", ["234"])
    return any(str(phone).startswith(f"+{code}") for code in codes)


def recently_texted(phone, purpose, *, since) -> int:
    """How many texts for ``purpose`` went to ``phone`` after ``since``."""
    from communications.models import SmsMessage

    return SmsMessage.objects.filter(to=phone, purpose=purpose,
                                     created_at__gte=since).count()


def _daily_limit_reached() -> bool:
    from communications.models import SmsMessage

    limit = getattr(settings, "SMS_DAILY_LIMIT", 300)
    sent_today = SmsMessage.objects.filter(
        created_at__gte=timezone.now() - timedelta(days=1)).count()
    return sent_today >= limit


def send_sms(to, body, *, purpose, cooperative=None) -> bool:
    """Send one text. Returns whether the provider accepted it.

    Never raises: a text that does not go is logged, and the caller carries
    on with the other ways in (an officer can still read a code out).
    """
    from communications.models import SmsMessage

    if not can_text(to):
        return False
    if _daily_limit_reached():
        logger.error("SMS daily limit reached; not texting %s (%s)",
                     to, purpose)
        return False

    backend = settings.SMS_BACKEND
    record = SmsMessage.objects.create(to=to, purpose=purpose,
                                       backend=backend,
                                       cooperative=cooperative)
    try:
        provider_id = _deliver(backend, to, body)
    except SmsError as exc:
        logger.warning("SMS to %s failed (%s): %s", to, purpose, exc)
        record.status = SmsMessage.Status.FAILED
        record.error = str(exc)[:255]
        record.save(update_fields=["status", "error", "updated_at"])
        return False
    record.status = SmsMessage.Status.SENT
    record.provider_id = provider_id[:64]
    record.save(update_fields=["status", "provider_id", "updated_at"])
    return True


def _deliver(backend, to, body) -> str:
    if backend == "termii":
        return _termii(to, body)
    if backend == "locmem":
        outbox.append({"to": to, "body": body})
        return f"locmem-{len(outbox)}"
    if backend == "console":
        # Printed, like Django's console mail backend: the log level in
        # development hides INFO.
        sys.stdout.write(f"SMS to {to}: {body}\n")
        sys.stdout.flush()
        return "console"
    raise SmsError(f"Unknown SMS backend {backend!r}.")


def _termii(to, body) -> str:
    """Send through Termii; returns its message id."""
    base = settings.TERMII_BASE_URL.rstrip("/")
    try:
        resp = requests.post(
            f"{base}/api/sms/send",
            json={
                "api_key": settings.TERMII_API_KEY,
                # Termii wants the international number without the plus.
                "to": str(to).lstrip("+"),
                "from": settings.TERMII_SENDER_ID,
                "sms": body,
                "type": "plain",
                # "dnd" reaches numbers on Nigeria's do-not-disturb list,
                # which most are; "generic" does not.
                "channel": settings.TERMII_CHANNEL,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise SmsError(f"Could not reach Termii: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if resp.status_code >= 400 or not message_id:
        message = data.get("message") if isinstance(data, dict) else None
        raise SmsError(message or f"Termii answered HTTP {resp.status_code}.")
    return str(message_id)
