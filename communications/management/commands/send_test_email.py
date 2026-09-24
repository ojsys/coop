"""Prove the mail configuration works, without waiting for a real flow.

Transactional mail fails *quietly* by design. A password reset always answers
"if that address has an account, a reset link is on its way" — it must, or the
endpoint becomes a way to test which addresses are registered — so a wrong
relay password looks exactly like a working one from the browser.

This command removes the guesswork. It prints what the app resolved, sends one
real message, and reports what the server said:

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        /home/USER/virtualenv/coop/3.11/bin/python manage.py send_test_email you@example.com

Run it against ``config.settings.prod``. Under the dev settings the console
backend prints to stdout and proves nothing about delivery — the command says
so rather than reporting a misleading success.
"""
from __future__ import annotations

import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.management.base import BaseCommand, CommandError

# The errors a relay actually returns, and what each one means in practice.
HINTS = (
    ("authentication", "The SMTP username or password is wrong. EMAIL_HOST_USER "
                       "is your Brevo SMTP *login*, which is not always your "
                       "account email, and the password is the generated SMTP "
                       "key rather than your dashboard password."),
    ("sender", "The From address is not a verified sender on the relay. Verify "
               "DEFAULT_FROM_EMAIL in Brevo under Senders & Domains."),
    ("not authorized", "The relay refused to send on behalf of this From "
                       "address. Verify DEFAULT_FROM_EMAIL as a sender in Brevo."),
    ("unverified", "The From address or its domain is not verified with the "
                   "relay."),
    ("timed out", "Could not reach the relay. Shared hosts often block outbound "
                  "SMTP — try port 587, and check the host allows it at all."),
    ("connection refused", "Nothing is listening at EMAIL_HOST:EMAIL_PORT. Check "
                           "both, and that EMAIL_HOST is not empty."),
    ("name or service not known", "EMAIL_HOST does not resolve. Check it for "
                                  "typos (smtp-relay.brevo.com)."),
    ("certificate", "TLS negotiation failed. Brevo expects EMAIL_USE_TLS=true on "
                    "port 587."),
)


def _mask(value: str) -> str:
    """Never print a credential; say only whether one is present."""
    if not value:
        return "NOT SET"
    return f"set ({len(value)} characters)"


class Command(BaseCommand):
    help = "Send one test email and report exactly why it failed, if it did."

    def add_arguments(self, parser):
        parser.add_argument("recipient", help="Where to send the test message.")
        parser.add_argument(
            "--from", dest="sender", default=None, metavar="ADDRESS",
            help="Override DEFAULT_FROM_EMAIL for this one send, to test "
                 "whether an unverified sender is the problem.")

    def handle(self, *args, **options):
        recipient = options["recipient"]
        sender = options["sender"] or settings.DEFAULT_FROM_EMAIL
        backend = settings.EMAIL_BACKEND

        self.stdout.write("Resolved mail configuration")
        self.stdout.write(f"  EMAIL_BACKEND       {backend}")
        self.stdout.write(f"  EMAIL_HOST          {getattr(settings, 'EMAIL_HOST', '') or 'NOT SET'}")
        self.stdout.write(f"  EMAIL_PORT          {getattr(settings, 'EMAIL_PORT', '')}")
        self.stdout.write(f"  EMAIL_USE_TLS       {getattr(settings, 'EMAIL_USE_TLS', '')}")
        self.stdout.write(f"  EMAIL_HOST_USER     {getattr(settings, 'EMAIL_HOST_USER', '') or 'NOT SET'}")
        self.stdout.write(f"  EMAIL_HOST_PASSWORD {_mask(getattr(settings, 'EMAIL_HOST_PASSWORD', ''))}")
        self.stdout.write(f"  DEFAULT_FROM_EMAIL  {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"  SITE_URL            {settings.SITE_URL}")
        self.stdout.write("")

        if "smtp" not in backend:
            self.stdout.write(self.style.WARNING(
                f"This is not the SMTP backend, so nothing will be delivered.\n"
                f"Re-run with DJANGO_SETTINGS_MODULE=config.settings.prod to "
                f"test real delivery."
            ))

        if "smtp" in backend and not getattr(settings, "EMAIL_HOST", ""):
            raise CommandError(
                "EMAIL_HOST is empty while the SMTP backend is selected, so "
                "Django would try to reach a mail server on localhost. Set "
                "EMAIL_HOST in backend/.env (smtp-relay.brevo.com for Brevo)."
            )

        self._warn_on_domain_mismatch(sender)

        message = EmailMultiAlternatives(
            subject="CooperativeOS test email",
            body=(
                "This is a test message from CooperativeOS.\n\n"
                "If you are reading it, the relay accepted mail from "
                f"{sender} and delivered it. Password resets, welcome mail, "
                "receipts and reminders all travel the same path."
            ),
            from_email=sender,
            to=[recipient],
            connection=get_connection(fail_silently=False),
        )

        self.stdout.write(f"Sending to {recipient} as {sender} …")
        try:
            accepted = message.send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001 - the whole point is to report it
            self._explain(exc)
            raise CommandError("The message was not sent.") from exc

        if not accepted:
            raise CommandError(
                "The server accepted the connection but sent nothing. This is "
                "almost always a rejected sender address."
            )

        self.stdout.write(self.style.SUCCESS(
            f"Accepted by the server for {recipient}."
        ))
        self.stdout.write(
            "Acceptance is not the same as arrival — if it does not appear, "
            "check spam, then the relay's own activity log. Mail from a domain "
            "without SPF and DKIM records is frequently filtered silently."
        )

    def _warn_on_domain_mismatch(self, sender: str) -> None:
        """Flag the failure that survives a successful send.

        A relay accepts the message first and decides whether you may send as
        that address afterwards, so a foreign From domain produces exactly the
        reading below: "accepted", then nothing ever arrives.
        """
        from email.utils import parseaddr
        from urllib.parse import urlparse

        from_domain = parseaddr(sender)[1].rsplit("@", 1)[-1]
        site_host = (urlparse(settings.SITE_URL).hostname or "").removeprefix("www.")
        if not (from_domain and site_host):
            return
        if (from_domain == site_host
                or from_domain.endswith("." + site_host)
                or site_host.endswith("." + from_domain)):
            return

        self.stdout.write(self.style.WARNING(
            f"Note: sending as {from_domain} while the site is {site_host}.\n"
            f"If {from_domain} is not verified with your relay, the message "
            f"below will be accepted and then dropped — check the relay's "
            f"activity log rather than trusting the success line."
        ))
        self.stdout.write("")

    def _explain(self, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"
        self.stdout.write(self.style.ERROR(f"Delivery failed — {detail}"))

        haystack = detail.lower()
        for needle, hint in HINTS:
            if needle in haystack:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING(f"Likely cause: {hint}"))
                return

        if isinstance(exc, smtplib.SMTPException):
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "The relay rejected the message. Its reply is quoted above — "
                "the relay's activity log usually explains it in full."
            ))
