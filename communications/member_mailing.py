"""
Email from a society's officers to its members.

Who receives a message is decided when it is sent, not when it is drafted:
:func:`resolve_recipients` reads the register as it stands then. A member who
has opted out of email (``ChannelPreference``) is left out and counted, never
written to; so is one with no address on file.

Audiences:

* everyone active on the register (non-members included — they are people
  the society serves), or everyone active holding one role;
* members repaying a loan, or behind on a repayment — for recovery. These
  ignore the member's status: a suspended member who owes is exactly who a
  recovery letter is for;
* members picked by hand, whatever their status.

Placeholders are filled in per member at the moment of sending, so a recovery
letter quotes the balance as it is, not as it was when the draft was saved.

Delivery is in slices like platform email (``platform_admin.mailing``): the
console calls :func:`deliver_pending` until nothing is pending, and
``manage.py send_member_emails`` finishes anything left behind.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from django.core.mail import get_connection
from django.db import transaction
from django.utils import timezone
from django.utils.safestring import mark_safe

from communications.formatting import render_body
from communications.models import MemberEmail, MemberEmailRecipient

DEFAULT_BUDGET_SECONDS = 15

# What an officer can write into a message, and what each becomes. Loan
# figures are worked out only when a message uses one.
PLACEHOLDERS = ("name", "first_name", "member_no", "cooperative",
                "loan_balance", "amount_overdue", "next_due_date")
_LOAN_PLACEHOLDERS = ("loan_balance", "amount_overdue", "next_due_date")


class MailingError(Exception):
    """A message that cannot be sent as it stands."""


@dataclass(frozen=True)
class Recipient:
    address: str
    name: str
    member_no: str
    membership_id: int
    role_name: str
    status: str


@dataclass(frozen=True)
class Resolved:
    recipients: list[Recipient]
    opted_out: int
    no_email: int


# ── Who it goes to ─────────────────────────────────────────────────────────
def audience_members(cooperative, *, audience, role_id=None, member_ids=()):
    """The memberships an audience covers, before opt-outs."""
    from accounts.models import Membership
    from loans.models import Loan, RepaymentInstalment

    qs = Membership.all_objects.filter(cooperative=cooperative)
    active = Membership.Status.ACTIVE
    if audience == MemberEmail.Audience.ALL:
        qs = qs.filter(status=active)
    elif audience == MemberEmail.Audience.ROLE:
        qs = qs.filter(status=active, role_id=role_id) if role_id else qs.none()
    elif audience == MemberEmail.Audience.BORROWERS:
        qs = qs.filter(pk__in=Loan.all_objects.filter(
            cooperative=cooperative, status=Loan.Status.DISBURSED,
        ).values("membership_id"))
    elif audience == MemberEmail.Audience.OVERDUE:
        qs = qs.filter(pk__in=RepaymentInstalment.all_objects.filter(
            cooperative=cooperative, loan__status=Loan.Status.DISBURSED,
            due_date__lt=timezone.localdate(),
        ).exclude(status=RepaymentInstalment.Status.PAID)
            .values("loan__membership_id"))
    elif audience == MemberEmail.Audience.SELECTED:
        qs = qs.filter(pk__in=list(member_ids))
    else:
        qs = qs.none()
    return (qs.select_related("user", "role", "channel_preference")
            .order_by("user__full_name", "member_no"))


def resolve_recipients(cooperative, *, audience, role_id=None,
                       member_ids=()) -> Resolved:
    """Every member a message with these settings would go to, in order."""
    from communications.services import _preference_for

    seen: set[str] = set()
    out: list[Recipient] = []
    opted_out = no_email = 0
    for m in audience_members(cooperative, audience=audience, role_id=role_id,
                              member_ids=member_ids):
        address = (m.user.email or "").strip()
        if not address:
            no_email += 1
            continue
        if not _preference_for(m).allows("email"):
            opted_out += 1
            continue
        key = address.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(Recipient(
            address=address, name=m.user.full_name, member_no=m.member_no,
            membership_id=m.pk, role_name=m.role.name if m.role else "",
            status=m.status))
    return Resolved(recipients=out, opted_out=opted_out, no_email=no_email)


def picked_member_ids(email: MemberEmail) -> list[int]:
    """The hand-picked members, read through the join table: ``email.members``
    goes through the tenant-scoped manager, which is empty under cron."""
    return list(MemberEmail.members.through.objects
                .filter(memberemail_id=email.pk)
                .values_list("membership_id", flat=True))


def recipients_for(email: MemberEmail) -> Resolved:
    member_ids = picked_member_ids(email)
    return resolve_recipients(email.cooperative, audience=email.audience,
                              role_id=email.audience_role_id,
                              member_ids=member_ids)


# ── What it says ───────────────────────────────────────────────────────────
def _money(value) -> str:
    return f"₦{Decimal(value or 0):,.2f}"


def loan_facts(membership) -> dict[str, str]:
    """The member's loan position, as the placeholders print it."""
    from loans.models import Loan, RepaymentInstalment

    loans = list(Loan.all_objects.filter(membership=membership,
                                         status=Loan.Status.DISBURSED))
    unpaid = (RepaymentInstalment.all_objects
              .filter(loan__in=loans)
              .exclude(status=RepaymentInstalment.Status.PAID)
              .order_by("due_date"))
    today = timezone.localdate()
    overdue = sum((i.outstanding for i in unpaid if i.due_date < today),
                  Decimal("0"))
    upcoming = next((i for i in unpaid if i.due_date >= today), None)
    return {
        "loan_balance": _money(sum((ln.outstanding for ln in loans),
                                   Decimal("0"))),
        "amount_overdue": _money(overdue),
        "next_due_date": (f"{upcoming.due_date.day} "
                          f"{upcoming.due_date:%B %Y}"
                          if upcoming else "—"),
    }


def values_for(email: MemberEmail, *, name="", member_no="",
               membership=None) -> dict[str, str]:
    values = {
        "name": name or "there",
        "first_name": (name.split()[0] if name.strip() else "there"),
        "member_no": member_no or "—",
        "cooperative": email.cooperative.name,
    }
    text = email.subject + email.body
    if any("{{" + p + "}}" in text for p in _LOAN_PLACEHOLDERS):
        if membership is not None:
            values.update(loan_facts(membership))
        else:
            values.update({"loan_balance": _money(0),
                           "amount_overdue": _money(0),
                           "next_due_date": "—"})
    return values


def personalise(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def send_one(email: MemberEmail, *, address, values,
             connection=None) -> bool:
    from communications.email import send_branded_email

    coop = email.cooperative
    return send_branded_email(
        to=address, subject=personalise(email.subject, values),
        template="emails/member_message.html", cooperative=coop,
        connection=connection, reply_to=coop.contact_email or None,
        context={"full_name": values.get("name", ""),
                 "body_html": mark_safe(  # noqa: S308 - escaped in render_body
                     render_body(personalise(email.body, values)))},
    )


# ── Sending ────────────────────────────────────────────────────────────────
@transaction.atomic
def start_sending(email: MemberEmail, *, actor) -> int:
    """Fix the recipient list and mark the message as sending.

    Returns how many recipients it has. Sending is once only.
    """
    original, email = email, MemberEmail.all_objects.select_for_update().get(
        pk=email.pk)
    if email.status != MemberEmail.Status.DRAFT:
        raise MailingError("This email has already been sent.")
    if not email.subject.strip() or not email.body.strip():
        raise MailingError("Write a subject and a message first.")
    if email.audience == MemberEmail.Audience.ROLE and not email.audience_role_id:
        raise MailingError("Choose a role.")

    resolved = recipients_for(email)
    if not resolved.recipients:
        raise MailingError("Nobody to send to: no member in this audience "
                           "has an email address and accepts email.")

    MemberEmailRecipient.all_objects.bulk_create([
        MemberEmailRecipient(
            cooperative_id=email.cooperative_id, email=email,
            address=r.address, name=r.name, member_no=r.member_no,
            membership_id=r.membership_id)
        for r in resolved.recipients
    ])
    email.status = MemberEmail.Status.SENDING
    email.sent_by = actor
    email.sent_at = timezone.now()
    email.save(update_fields=["status", "sent_by", "sent_at", "updated_at"])

    from audit.services import record_action
    record_action(cooperative=email.cooperative, actor=actor,
                  action=f'Emailed members "{email.subject}"', entity=email,
                  after={"audience": email.audience, "purpose": email.purpose,
                         "recipients": len(resolved.recipients)})
    original.refresh_from_db()
    return len(resolved.recipients)


def _pending(email):
    return MemberEmailRecipient.all_objects.filter(
        email=email, status=MemberEmailRecipient.Status.PENDING)


def deliver_pending(email: MemberEmail, *,
                    budget: float = DEFAULT_BUDGET_SECONDS) -> int:
    """Send to pending recipients for up to ``budget`` seconds.

    Returns how many are still pending. Marks the message sent once none are.
    """
    deadline = time.monotonic() + budget
    connection = get_connection()
    try:
        connection.open()
    except Exception:  # noqa: BLE001 - each send logs its own failure
        connection = None
    try:
        for row in _pending(email).select_related("membership").iterator():
            values = values_for(email, name=row.name, member_no=row.member_no,
                                membership=row.membership)
            ok = send_one(email, address=row.address, values=values,
                          connection=connection)
            row.status = (MemberEmailRecipient.Status.SENT if ok
                          else MemberEmailRecipient.Status.FAILED)
            row.sent_at = timezone.now() if ok else None
            row.save(update_fields=["status", "sent_at", "updated_at"])
            if time.monotonic() >= deadline:
                break
    finally:
        if connection is not None:
            connection.close()

    remaining = _pending(email).count()
    if remaining == 0 and email.status == MemberEmail.Status.SENDING:
        email.status = MemberEmail.Status.SENT
        email.finished_at = timezone.now()
        email.save(update_fields=["status", "finished_at", "updated_at"])
    return remaining


def retry_failed(email: MemberEmail) -> int:
    """Queue the failed recipients again. Returns how many."""
    if email.status == MemberEmail.Status.DRAFT:
        raise MailingError("This email has not been sent yet.")
    count = MemberEmailRecipient.all_objects.filter(
        email=email, status=MemberEmailRecipient.Status.FAILED).update(
            status=MemberEmailRecipient.Status.PENDING)
    if count:
        email.status = MemberEmail.Status.SENDING
        email.finished_at = None
        email.save(update_fields=["status", "finished_at", "updated_at"])
    return count
