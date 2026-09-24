"""
Deployment system checks.

A missing SPA build is otherwise a *silent* failure: WhiteNoise logs a single
``UserWarning: No directory at: ...`` to stderr, the app boots normally, the
admin and API work — and every non-API route returns 404 with nothing obvious
to point at. Surfacing it through ``manage.py check`` turns that into something
you see before restarting, rather than after.
"""
from __future__ import annotations

from pathlib import Path

from django.core.checks import Warning, register


@register()
def email_delivery_configured(app_configs, **kwargs):
    """Warn when transactional mail cannot possibly be delivered.

    Mail failing is invisible from the outside: the password-reset endpoint
    answers the same way whether or not it sent anything, because a differing
    response would let anyone test which addresses are registered. So a blank
    relay host looks exactly like a working one until somebody notices no mail
    has arrived for a week. ``manage.py check`` runs before every restart,
    which is a far better place to find out.

    Only meaningful for the SMTP backend — under the console backend in
    development, sending nothing is the point.
    """
    import os

    from django.conf import settings

    backend = getattr(settings, "EMAIL_BACKEND", "")
    if "smtp" not in backend:
        return []

    problems = []

    missing = [
        name for name in ("EMAIL_HOST", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD")
        if not getattr(settings, name, "")
    ]
    if missing:
        problems.append(
            Warning(
                "SMTP email is selected but not fully configured: "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                "empty.",
                hint=(
                    "Set these in backend/.env. For Brevo, EMAIL_HOST is "
                    "smtp-relay.brevo.com, EMAIL_HOST_USER is the SMTP login "
                    "from your account (not necessarily your sign-in email) "
                    "and EMAIL_HOST_PASSWORD is the generated SMTP key. With "
                    "EMAIL_HOST empty Django tries localhost, where nothing "
                    "is listening. Verify with `manage.py send_test_email`."
                ),
                id="core.W002",
            )
        )

    # Keyed on whether it was configured at all, not on the address itself:
    # the bundled default may legitimately be the deployment's own domain.
    if os.environ.get("DEFAULT_FROM_EMAIL") is None:
        problems.append(
            Warning(
                "DEFAULT_FROM_EMAIL is not set, so mail goes out as "
                f"{settings.DEFAULT_FROM_EMAIL!r}.",
                hint=(
                    "Relays refuse to send on behalf of an address they have "
                    "not verified, and reject it at SMTP time — which is the "
                    "most common reason mail silently never arrives. Set "
                    "DEFAULT_FROM_EMAIL to a sender you have verified with "
                    "your relay, and add SPF and DKIM records for its domain "
                    "so it is not then filtered as spam."
                ),
                id="core.W003",
            )
        )

    return problems


@register()
def frontend_build_present(app_configs, **kwargs):
    """Warn when FRONTEND_DIST has no index.html to serve."""
    from django.conf import settings

    dist = Path(settings.FRONTEND_DIST)
    if (dist / "index.html").is_file():
        return []

    return [
        Warning(
            f"No SPA build at {dist}",
            hint=(
                'Django serves "/" from this directory, so every non-API route '
                "will return 404 until it exists. Build it with `npm run build` "
                "in frontend/, upload the output, and either place it at one of "
                "the default locations or set the FRONTEND_DIST environment "
                "variable to where it actually lives."
            ),
            id="core.W001",
        )
    ]
