"""
Branded transactional email.

Everything user-facing goes through :func:`send_branded_email`, which renders a
society-branded HTML message plus a plain-text alternative (many members read
mail on low-end Android clients, and some corporate filters strip HTML).

Two deliberate choices:

* **Failures are logged, never swallowed.** The previous helper passed
  ``fail_silently=True``, so a wrong Brevo password looked exactly like working
  mail. Delivery problems now land in the ``api.errors`` log.
* **The society's own brand is used when there is one.** A member of Ìmọ̀lè MCS
  should get mail from Ìmọ̀lè, not from CooperativeOS.
"""
from __future__ import annotations

import logging
from html import unescape

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.html import strip_tags
from django.utils.http import urlsafe_base64_encode

logger = logging.getLogger("api.errors")

PLATFORM_FALLBACK_NAME = "CooperativeOS"


def _timeout_hours() -> int:
    return max(1, settings.PASSWORD_RESET_TIMEOUT // 3600)


def _absolute(url_path: str) -> str:
    """Build an absolute URL for a link in an email."""
    return f"{settings.SITE_URL.rstrip('/')}{url_path}"


def brand_context(cooperative=None) -> dict:
    """Header/footer values for the base template.

    Falls back to the platform's own profile so mail sent outside any tenant
    (a platform admin resetting their password) is still branded.
    """
    if cooperative is not None:
        contact = " · ".join(filter(None, (
            cooperative.contact_email, cooperative.contact_phone,
        )))
        return {
            "brand_name": cooperative.name,
            "brand_subtitle": "Cooperative Society",
            "logo_url": (_absolute(cooperative.logo.url)
                         if cooperative.logo else ""),
            "contact_line": contact,
        }

    try:
        from platform_admin.models import PlatformProfile

        profile = PlatformProfile.load()
        return {
            "brand_name": profile.name or PLATFORM_FALLBACK_NAME,
            "brand_subtitle": "",
            "logo_url": _absolute(profile.logo.url) if profile.logo else "",
            "contact_line": profile.support_email,
        }
    except Exception:  # noqa: BLE001 - branding must never block delivery
        logger.warning("Could not load platform branding for an email",
                       exc_info=True)
        return {"brand_name": PLATFORM_FALLBACK_NAME, "brand_subtitle": "",
                "logo_url": "", "contact_line": ""}


def send_branded_email(*, to, subject, template, context=None,
                       cooperative=None) -> bool:
    """Render and send one message. Returns whether it was accepted.

    Never raises: a failed notification must not roll back the business action
    that triggered it (a recorded contribution, an approved loan).
    """
    if not to:
        return False

    payload = {"subject": subject, **brand_context(cooperative),
               **(context or {})}
    try:
        html = render_to_string(template, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Could not render email template %s", template)
        return False

    # strip_tags removes markup but leaves entities encoded, so an escaped
    # ampersand survives into the text part: a reset link arrives as
    # "?uid=MQ&amp;token=..." and the token is read as "amp;token", which
    # breaks the link for anyone whose client prefers plain text. The HTML
    # part is unaffected, so this fails quietly for a subset of readers.
    text = unescape(strip_tags(html))
    # Collapse the whitespace the HTML layout leaves behind.
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())

    message = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to] if isinstance(to, str) else list(to),
    )
    message.attach_alternative(html, "text/html")

    try:
        sent = message.send(fail_silently=False)
    except Exception:  # noqa: BLE001 - SMTP is a remote dependency
        logger.exception("Email delivery failed: %s -> %s", subject, to)
        return False

    if not sent:
        logger.error("Email was not accepted by the server: %s -> %s",
                     subject, to)
    return bool(sent)


def platform_support_email() -> str:
    """Where operational alerts go when there is no society involved."""
    try:
        from platform_admin.models import PlatformProfile

        return PlatformProfile.load().support_email
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the platform support address",
                       exc_info=True)
        return settings.DEFAULT_FROM_EMAIL


def send_alert_email(*, to, subject, heading, intro="", rows=None,
                     cooperative=None, cta_url="", cta_label="") -> bool:
    """Tell officers (or the platform team) that something needs attention.

    ``rows`` is a list of ``(label, value)`` pairs rendered as a detail table —
    these are read quickly on a phone, so the facts carry the message.
    """
    return send_branded_email(
        to=to,
        subject=subject,
        template="emails/alert.html",
        cooperative=cooperative,
        context={"heading": heading, "intro": intro, "rows": rows or [],
                 "cta_url": cta_url, "cta_label": cta_label},
    )


# ── Account emails ──────────────────────────────────────────────────────────
def _reset_link(user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return _absolute(f"/reset-password?uid={uid}&token={token}")


def send_password_reset_email(user, cooperative=None) -> bool:
    return send_branded_email(
        to=user.email,
        subject="Reset your password",
        template="emails/password_reset.html",
        cooperative=cooperative,
        context={
            "full_name": user.full_name,
            "email": user.email,
            "cta_url": _reset_link(user),
            "cta_label": "Choose a new password",
            "timeout_hours": _timeout_hours(),
        },
    )


def send_welcome_email(membership) -> bool:
    """Invite a newly added member to set their own password.

    Carries a set-password link rather than a generated password — a password
    sent by email lives forever in the member's inbox.
    """
    user = membership.user
    cooperative = membership.cooperative
    return send_branded_email(
        to=user.email,
        subject=f"Welcome to {cooperative.name}",
        template="emails/welcome.html",
        cooperative=cooperative,
        context={
            "full_name": user.full_name,
            "member_no": membership.member_no,
            "cta_url": _reset_link(user),
            "cta_label": "Set your password",
            "timeout_hours": _timeout_hours(),
        },
    )
