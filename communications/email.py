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
                       cooperative=None, connection=None) -> bool:
    """Render and send one message. Returns whether it was accepted.

    Never raises: a failed notification must not roll back the business action
    that triggered it (a recorded contribution, an approved loan).

    ``connection`` lets a batch share one SMTP session instead of logging in
    to the mail server once per message.
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
        connection=connection,
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


def cooperative_notification_emails(cooperative) -> list[str]:
    """Where mail addressed to a cooperative itself should go.

    Its published contact address plus every privileged officer — the contact
    address is often the applicant's, and by go-live the people who need to
    act on it may be different. De-duplicated case-insensitively so an officer
    who is also the contact is not mailed twice.
    """
    from accounts.models import Membership

    addresses = []
    if cooperative.contact_email:
        addresses.append(cooperative.contact_email)

    officers = (
        Membership.all_objects
        .filter(cooperative=cooperative, status=Membership.Status.ACTIVE)
        .select_related("user", "role")
    )
    addresses += [m.user.email for m in officers
                  if m.role and m.role.is_privileged and m.user.email]

    seen, unique = set(), []
    for address in addresses:
        key = address.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(address.strip())
    return unique


def send_go_live_email(cooperative) -> bool:
    """Announce to a cooperative that its account is live.

    Returns False when there is nobody to tell, which is worth logging: a
    cooperative with no contact address and no privileged officer has gone
    live with no one informed.
    """
    recipients = cooperative_notification_emails(cooperative)
    if not recipients:
        logger.warning(
            "No contact address or privileged officer for %s — the go-live "
            "announcement could not be sent", cooperative,
        )
        return False

    return send_branded_email(
        to=recipients,
        subject=f"{cooperative.name} is now live",
        template="emails/go_live.html",
        cooperative=cooperative,
        context={
            "coop_name": cooperative.name,
            "cta_url": _absolute("/login"),
            "cta_label": "Sign in to your cooperative",
        },
    )


def send_welcome_email(membership) -> bool:
    """Invite a newly added member to set their own password.

    Carries a set-password link rather than a generated password — a password
    sent by email lives forever in the member's inbox.
    """
    user = membership.user
    cooperative = membership.cooperative
    if user.has_usable_password():
        # Already on CooperativeOS (a member of another society): nothing to
        # set up. They sign in as usual and switch to this society.
        return send_branded_email(
            to=user.email,
            subject=f"Welcome to {cooperative.name}",
            template="emails/welcome.html",
            cooperative=cooperative,
            context={
                "full_name": user.full_name,
                "member_no": membership.member_no,
                "existing_account": True,
                "cooperative_name": cooperative.name,
                "cta_url": _absolute("/app"),
                "cta_label": f"Open {cooperative.name}",
            },
        )
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


def _verify_email_link(user) -> str:
    from accounts.email_verification import make_token

    return _absolute(f"/verify-email?token={make_token(user)}")


def send_email_verification(user) -> bool:
    """Ask a person to confirm their address — the resend path."""
    return send_branded_email(
        to=user.email,
        subject="Confirm your email address",
        template="emails/verify_email.html",
        context={"full_name": user.full_name,
                 "cta_url": _verify_email_link(user),
                 "cta_label": "Confirm my email"},
    )


def send_signup_welcome_email(membership) -> bool:
    """Welcome the founder of a society that has just signed up.

    It is live already, so this is not an announcement but a start: when the
    trial ends, what to do first, and a link confirming their address — the
    one thing submitting KYC waits for. An address already confirmed (they
    hold an account elsewhere) gets the console link instead.
    """
    from core.entitlements import subscription_for

    user = membership.user
    cooperative = membership.cooperative
    sub = subscription_for(cooperative)
    verified = user.email_verified_at is not None
    return send_branded_email(
        to=user.email,
        subject=f"{cooperative.name} is ready on CooperativeOS",
        template="emails/signup_welcome.html",
        context={
            "full_name": user.full_name,
            "coop_name": cooperative.name,
            "trial_ends": (sub.current_period_end
                           if sub and sub.status == sub.Status.TRIAL
                           else None),
            "needs_confirmation": not verified,
            "cta_url": (_absolute("/console") if verified
                        else _verify_email_link(user)),
            "cta_label": ("Open your console" if verified
                          else "Confirm my email"),
        },
    )


def send_trial_ending_email(subscription, *, days_left: int) -> bool:
    """Tell a society its free trial ends soon, and what happens then.

    Mentions verification while it is outstanding: a society that reaches the
    end of its trial without it has been keeping books but not taking money,
    and that is the step most worth a nudge.
    """
    from tenants.verification import is_verified

    cooperative = subscription.cooperative
    recipients = cooperative_notification_emails(cooperative)
    if not recipients:
        return False
    ends = subscription.current_period_end
    when = "tomorrow" if days_left == 1 else f"in {days_left} days"
    amount = subscription.cycle_amount
    intro = (f"Your free trial of CooperativeOS ends {when}, on "
             f"{ends.day} {ends:%B %Y}. After that we send an invoice for the "
             f"{subscription.plan.name} plan")
    intro += (f" ({subscription.plan.currency} {amount:,.2f} per "
              f"{'year' if subscription.is_annual else 'month'})."
              if amount is not None else ".")
    if not is_verified(cooperative):
        intro += (" You have not completed verification yet — until you do, "
                  "members cannot pay online and loans cannot be paid out "
                  "electronically.")
    return send_alert_email(
        to=recipients, cooperative=cooperative,
        subject=f"{cooperative.name}: your free trial ends {when}",
        heading=f"Your free trial ends {when}",
        intro=intro,
        cta_url=_absolute("/console"),
        cta_label="Open the console",
    )


def send_subscription_invoice_email(invoice, *, is_reminder: bool = False,
                                    suspend_on=None) -> bool:
    """Send a cooperative its subscription invoice, or a reminder for it.

    One function for both so the figures can never disagree between the first
    notice and the tenth. Returns False when there is nobody to bill — worth
    logging, because an invoice nobody is told about will never be paid and the
    cooperative would be suspended for silence we caused.
    """
    cooperative = invoice.cooperative
    recipients = cooperative_notification_emails(cooperative)
    if not recipients:
        logger.warning(
            "No contact address or privileged officer for %s — subscription "
            "invoice %s could not be sent", cooperative, invoice.number,
        )
        return False

    plan = invoice.subscription.plan if invoice.subscription_id else None
    amount = f"{invoice.currency} {invoice.amount:,.2f}"
    subject = (
        f"Reminder: {cooperative.name} subscription {invoice.period_label}"
        if is_reminder else
        f"{cooperative.name} subscription invoice — {invoice.period_label}"
    )

    return send_branded_email(
        to=recipients,
        subject=subject,
        template="emails/subscription_invoice.html",
        cooperative=cooperative,
        context={
            "coop_name": cooperative.name,
            "number": invoice.number,
            "period_label": invoice.period_label or "this period",
            "plan_name": plan.name if plan else "Subscription",
            "amount": amount,
            "due_at": invoice.due_at.strftime("%d %b %Y") if invoice.due_at else "on receipt",
            "is_reminder": is_reminder,
            "reminder_count": invoice.reminder_count,
            "was_trial": invoice.reminder_count == 0 and not is_reminder,
            "suspend_warning": suspend_on is not None,
            "suspend_on": suspend_on.strftime("%d %b %Y") if suspend_on else "",
            # The token is the authorisation, so the link exists only here.
            "cta_url": _absolute(f"/billing/pay/{invoice.pay_token}") if invoice.pay_token else "",
            "cta_label": "Pay this invoice",
        },
    )


def send_join_request_received_email(join_request) -> bool:
    """Acknowledge a membership request to the person who made it.

    Previously only the cooperative's officers were told, so an applicant had
    nothing but a web page that vanished when they closed the tab — and no way
    to tell a submitted request from a lost one.

    Addressed to a stranger, so it says nothing about the cooperative beyond
    the name they typed themselves, and is careful not to imply an account
    exists: none is created until an officer approves.
    """
    cooperative = join_request.cooperative
    message = (
        f"Thank you — your request to join {cooperative.name} has been "
        f"received.\n\n"
        f"What happens next:\n\n"
        f"1. An officer of the cooperative checks your details against their "
        f"member register.\n"
        f"2. If they admit you, we email you a link to choose your own "
        f"password.\n"
        f"3. You can sign in once you have set it.\n\n"
        f"No account exists for you yet, and you will never be sent a "
        f"password — only a link to choose one. If you did not make this "
        f"request, you can ignore this message: nothing has been created.\n\n"
        f"If you hear nothing, contact an officer of the cooperative "
        f"directly — they decide every request themselves."
    )
    return send_branded_email(
        to=join_request.email,
        subject=f"Your request to join {cooperative.name}",
        template="emails/notice.html",
        cooperative=cooperative,
        context={
            "full_name": join_request.full_name,
            "message": message,
        },
    )
