"""
Email from the platform team to cooperatives' officers.

Who receives a message is decided when it is sent, not when it is drafted:
:func:`resolve_recipients` reads each cooperative's *current* officers — every
active member whose role is privileged — plus, optionally, the cooperative's
published contact address. An address appears once per message however many
cooperatives it serves.

There is no worker process on shared hosting, so delivery is done in slices:
:func:`deliver_pending` sends for at most ``budget`` seconds and returns. The
platform's Email page calls it repeatedly until nothing is pending, and
``manage.py send_platform_emails`` finishes anything left behind if that page
is closed mid-send.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from html import escape

from django.core.mail import get_connection
from django.db import transaction
from django.utils import timezone
from django.utils.safestring import mark_safe

from platform_admin.models import (PlatformEmail, PlatformEmailRecipient,
                                   Subscription)

DEFAULT_BUDGET_SECONDS = 15
CONTACT_CAPACITY = "Contact address"


class MailingError(Exception):
    """A message that cannot be sent as it stands."""


@dataclass(frozen=True)
class Recipient:
    address: str
    name: str
    capacity: str
    cooperative_id: int | None
    cooperative_name: str
    user_id: int | None


# ── Who it goes to ─────────────────────────────────────────────────────────
def audience_cooperatives(*, audience, cooperative_ids=()):
    """The cooperatives an audience covers. Only live ones: a suspended or
    closed society's officers are not who platform news is for."""
    from tenants.models import Cooperative

    qs = Cooperative.objects.filter(status=Cooperative.Status.ACTIVE)
    if audience == PlatformEmail.Audience.TRIAL:
        qs = qs.filter(subscription__status=Subscription.Status.TRIAL)
    elif audience == PlatformEmail.Audience.UNVERIFIED:
        qs = qs.exclude(verification__status="verified")
    elif audience == PlatformEmail.Audience.SELECTED:
        qs = qs.filter(pk__in=list(cooperative_ids))
    return qs.order_by("name")


def resolve_recipients(*, audience, cooperative_ids=(), only_user_ids=(),
                       include_contact_email=True) -> list[Recipient]:
    """Every address a message with these settings would go to, in order."""
    from accounts.models import Membership

    coops = list(audience_cooperatives(audience=audience,
                                       cooperative_ids=cooperative_ids))
    officers = (
        Membership.all_objects
        .filter(cooperative__in=coops, status=Membership.Status.ACTIVE)
        .select_related("user", "role", "cooperative")
        .order_by("cooperative__name", "user__full_name")
    )
    narrowed = set(only_user_ids)

    seen: set[str] = set()
    out: list[Recipient] = []

    def add(recipient: Recipient):
        key = recipient.address.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(recipient)

    by_coop: dict[int, list] = {}
    for m in officers:
        if m.role and m.role.is_privileged and m.user.email:
            by_coop.setdefault(m.cooperative_id, []).append(m)

    for coop in coops:
        for m in by_coop.get(coop.id, []):
            if narrowed and m.user_id not in narrowed:
                continue
            add(Recipient(address=m.user.email.strip(),
                          name=m.user.full_name, capacity=m.role.name,
                          cooperative_id=coop.id, cooperative_name=coop.name,
                          user_id=m.user_id))
        # Picking particular officers means exactly those people.
        if include_contact_email and not narrowed and coop.contact_email:
            add(Recipient(address=coop.contact_email.strip(), name="",
                          capacity=CONTACT_CAPACITY, cooperative_id=coop.id,
                          cooperative_name=coop.name, user_id=None))
    return out


def recipients_for(email: PlatformEmail) -> list[Recipient]:
    return resolve_recipients(
        audience=email.audience,
        cooperative_ids=email.cooperatives.values_list("pk", flat=True),
        only_user_ids=email.only_users.values_list("pk", flat=True),
        include_contact_email=email.include_contact_email,
    )


# ── What it says ───────────────────────────────────────────────────────────
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_P = "margin:0 0 14px;"


def personalise(text: str, *, name: str, cooperative: str) -> str:
    return (text.replace("{{name}}", name or "there")
            .replace("{{cooperative}}", cooperative or "your cooperative"))


def _inline(line: str) -> str:
    """Escape first, then add the two marks — so nothing typed is ever HTML."""
    html = escape(line, quote=False)
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    return _LINK.sub(
        lambda m: f'<a href="{escape(m.group(2))}" '
                  f'style="color:#0b4f3a;">{m.group(1)}</a>', html)


def render_body(text: str) -> str:
    """The team's plain text as email-safe HTML: paragraphs, ``- `` bullets,
    ``**bold**`` and ``[text](https://…)`` links. Same marks as the legal
    pages, so the team writes one way everywhere."""
    blocks = re.split(r"\n\s*\n", text.strip())
    parts = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if lines and all(ln.lstrip().startswith("- ") for ln in lines):
            items = "".join(f'<li style="margin:0 0 6px;">'
                            f'{_inline(ln.lstrip()[2:])}</li>'
                            for ln in lines)
            parts.append(f'<ul style="{_P}padding-left:20px;">{items}</ul>')
        elif lines:
            parts.append(f'<p style="{_P}">'
                         + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return "".join(parts)


def send_one(email: PlatformEmail, *, address, name="", cooperative_name="",
             connection=None) -> bool:
    from communications.email import send_branded_email

    subject = personalise(email.subject, name=name,
                          cooperative=cooperative_name)
    body = personalise(email.body, name=name, cooperative=cooperative_name)
    return send_branded_email(
        to=address, subject=subject, template="emails/platform_message.html",
        connection=connection,
        context={"full_name": name,
                 "body_html": mark_safe(render_body(body))},  # noqa: S308 - escaped in render_body
    )


# ── Sending ────────────────────────────────────────────────────────────────
@transaction.atomic
def start_sending(email: PlatformEmail, *, actor) -> int:
    """Fix the recipient list and mark the message as sending.

    Returns how many recipients it has. Sending is once only: a message that
    has started cannot be re-targeted.
    """
    original, email = email, PlatformEmail.objects.select_for_update().get(
        pk=email.pk)
    if email.status != PlatformEmail.Status.DRAFT:
        raise MailingError("This email has already been sent.")
    if not email.subject.strip() or not email.body.strip():
        raise MailingError("Write a subject and a message first.")
    if (email.audience == PlatformEmail.Audience.SELECTED
            and not email.cooperatives.exists()):
        raise MailingError("Choose at least one cooperative.")

    recipients = recipients_for(email)
    if not recipients:
        raise MailingError("Nobody to send to: none of these cooperatives "
                           "has an officer or contact address with an email.")

    PlatformEmailRecipient.objects.bulk_create([
        PlatformEmailRecipient(
            email=email, address=r.address, name=r.name, capacity=r.capacity,
            cooperative_id=r.cooperative_id, user_id=r.user_id)
        for r in recipients
    ])
    email.status = PlatformEmail.Status.SENDING
    email.sent_by = actor
    email.sent_at = timezone.now()
    email.save(update_fields=["status", "sent_by", "sent_at", "updated_at"])

    from audit.services import record_action
    record_action(actor=actor, action="platform.email.send", entity=email,
                  after={"subject": email.subject, "audience": email.audience,
                         "recipients": len(recipients)})
    original.refresh_from_db()
    return len(recipients)


def deliver_pending(email: PlatformEmail, *,
                    budget: float = DEFAULT_BUDGET_SECONDS) -> int:
    """Send to pending recipients for up to ``budget`` seconds.

    Returns how many are still pending. Marks the message sent once none are.
    """
    deadline = time.monotonic() + budget
    pending = (email.recipients.filter(status=PlatformEmailRecipient.Status.PENDING)
               .select_related("cooperative"))
    connection = get_connection()
    try:
        connection.open()
    except Exception:  # noqa: BLE001 - each send logs its own failure
        connection = None
    try:
        for row in pending.iterator():
            ok = send_one(email, address=row.address, name=row.name,
                          cooperative_name=(row.cooperative.name
                                            if row.cooperative else ""),
                          connection=connection)
            row.status = (PlatformEmailRecipient.Status.SENT if ok
                          else PlatformEmailRecipient.Status.FAILED)
            row.sent_at = timezone.now() if ok else None
            row.save(update_fields=["status", "sent_at", "updated_at"])
            if time.monotonic() >= deadline:
                break
    finally:
        if connection is not None:
            connection.close()

    remaining = email.recipients.filter(
        status=PlatformEmailRecipient.Status.PENDING).count()
    if remaining == 0 and email.status == PlatformEmail.Status.SENDING:
        email.status = PlatformEmail.Status.SENT
        email.finished_at = timezone.now()
        email.save(update_fields=["status", "finished_at", "updated_at"])
    return remaining


def retry_failed(email: PlatformEmail) -> int:
    """Queue the failed recipients again. Returns how many."""
    if email.status == PlatformEmail.Status.DRAFT:
        raise MailingError("This email has not been sent yet.")
    count = email.recipients.filter(
        status=PlatformEmailRecipient.Status.FAILED).update(
            status=PlatformEmailRecipient.Status.PENDING)
    if count:
        email.status = PlatformEmail.Status.SENDING
        email.finished_at = None
        email.save(update_fields=["status", "finished_at", "updated_at"])
    return count
