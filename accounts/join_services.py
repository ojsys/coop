"""
Signing up: a society applying to join the platform, and a person applying to
join a society.

Both are initiated by strangers, so both are written defensively — no account
is created until a human approves, and neither flow reveals whether an address
or a society already exists beyond what is already public.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from accounts.join_models import JoinRequest
from accounts.models import Membership, Role, User
from core.reference_data import country_name

logger = logging.getLogger("api.errors")


class SignupError(Exception):
    """A signup could not be accepted (duplicate, already a member, ...)."""


def _unique_slug(name: str) -> str:
    """A URL-safe slug that doesn't collide with an existing society."""
    from tenants.models import Cooperative

    base = slugify(name)[:70] or "society"
    slug, n = base, 1
    while Cooperative.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base[:70 - len(str(n)) - 1]}-{n}"
    return slug


def _officer_emails(cooperative) -> list[str]:
    """Addresses of the society's privileged officers."""
    officers = (
        Membership.all_objects
        .filter(cooperative=cooperative, status=Membership.Status.ACTIVE)
        .select_related("user", "role")
    )
    return [m.user.email for m in officers
            if m.role and m.role.is_privileged and m.user.email]


# ── A society applying to join the platform ─────────────────────────────────
@transaction.atomic
def apply_for_cooperative(*, society_name, applicant_name, applicant_email,
                          applicant_phone="", country="", state="", lga="",
                          coop_type="", estimated_members=0, message=""):
    """Register an application and put it into the onboarding pipeline.

    The society is created PROSPECTIVE, not ACTIVE: it exists so the platform
    team has something to work with, but it cannot be used until go-live moves
    it to ACTIVE (see platform_admin.models.OnboardingItem.advance).
    """
    from platform_admin.services import start_onboarding
    from tenants.services import provision_cooperative

    cooperative = provision_cooperative(
        name=society_name,
        slug=_unique_slug(society_name),
        status="prospective",
        country=country,
        state=state,
        lga=lga,
        coop_type=coop_type,
        contact_email=applicant_email,
        contact_phone=applicant_phone,
    )

    item = start_onboarding(cooperative)
    note = (f"Applied via the website.\n"
            f"Contact: {applicant_name} <{applicant_email}> {applicant_phone}\n"
            f"Estimated members: {estimated_members or 'not stated'}")
    if message:
        note += f"\n\nThey said:\n{message}"
    item.notes = f"{item.notes}\n\n{note}".strip() if item.notes else note
    item.save(update_fields=["notes", "updated_at"])

    # Notifications must never roll back an accepted application.
    transaction.on_commit(lambda: _notify_application(
        cooperative=cooperative, applicant_name=applicant_name,
        applicant_email=applicant_email, applicant_phone=applicant_phone,
        estimated_members=estimated_members, message=message,
    ))
    return cooperative


def _notify_application(*, cooperative, applicant_name, applicant_email,
                        applicant_phone, estimated_members, message):
    from communications.email import (
        send_alert_email, send_branded_email, platform_support_email,
    )

    rows = [
        ("Society", cooperative.name),
        ("Contact", f"{applicant_name} <{applicant_email}>"),
        ("Phone", applicant_phone or "—"),
        ("Location", " · ".join(filter(None, (
            cooperative.lga, cooperative.state,
            country_name(cooperative.country)))) or "—"),
        ("Estimated members", str(estimated_members or "not stated")),
    ]
    if message:
        rows.append(("Message", message))

    send_branded_email(
        to=applicant_email,
        subject=f"We've received {cooperative.name}'s application",
        template="emails/coop_application_received.html",
        context={"full_name": applicant_name,
                 "society_name": cooperative.name, "rows": rows},
    )
    send_alert_email(
        to=platform_support_email(),
        subject=f"New cooperative application: {cooperative.name}",
        heading="A society has applied to join",
        intro="It has been created as PROSPECTIVE and added to the onboarding "
              "pipeline at the Discovery stage.",
        rows=rows,
    )


# ── A person applying to join a society ─────────────────────────────────────
@transaction.atomic
def request_to_join(*, cooperative, full_name, email, phone="", message=""):
    """Record a membership request for officers of that society to decide."""
    email = email.strip().lower()

    already_a_member = Membership.all_objects.filter(
        cooperative=cooperative, user__email__iexact=email,
    ).exists()
    if already_a_member:
        raise SignupError(
            "That email address already belongs to a member of this society. "
            "Try signing in, or use 'Forgot password'."
        )

    # Portable stand-in for the partial unique index MySQL cannot express.
    duplicate = JoinRequest.all_objects.filter(
        cooperative=cooperative, email__iexact=email,
        status=JoinRequest.Status.PENDING,
    ).exists()
    if duplicate:
        raise SignupError(
            "You already have a request waiting with this society. An officer "
            "will be in touch."
        )

    request = JoinRequest.all_objects.create(
        cooperative=cooperative, full_name=full_name, email=email,
        phone=phone, message=message,
    )

    transaction.on_commit(lambda: _notify_join_request(request))
    return request


def _notify_join_request(request):
    from communications.email import send_alert_email

    recipients = _officer_emails(request.cooperative)
    if not recipients:
        logger.warning(
            "Join request %s for %s has no privileged officer to notify",
            request.pk, request.cooperative,
        )
        return

    send_alert_email(
        to=recipients,
        subject=f"New membership request: {request.full_name}",
        heading="Someone has asked to join your society",
        intro="Review it under Members → Join requests in the console.",
        cooperative=request.cooperative,
        rows=[
            ("Name", request.full_name),
            ("Email", request.email),
            ("Phone", request.phone or "—"),
            ("Message", request.message or "—"),
        ],
    )


@transaction.atomic
def approve_join_request(join_request, *, actor, member_no, role=None):
    """Admit the applicant: create their account and membership.

    The account is created here — not when they applied — so an unapproved
    stranger never holds credentials for a society. They are emailed a
    set-password link rather than a generated password.
    """
    if join_request.status != JoinRequest.Status.PENDING:
        raise SignupError("This request has already been decided.")

    cooperative = join_request.cooperative

    if Membership.all_objects.filter(cooperative=cooperative,
                                     member_no=member_no).exists():
        raise SignupError(f"Member number {member_no} is already in use.")

    user = User.objects.filter(email__iexact=join_request.email).first()
    if user is None:
        # No usable password: the welcome email carries a set-password link.
        user = User.objects.create_user(
            email=join_request.email, full_name=join_request.full_name,
            phone=join_request.phone,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])

    if role is None:
        role = Role.all_objects.filter(cooperative=cooperative,
                                       slug=Role.MEMBER).first()

    membership = Membership.all_objects.create(
        cooperative=cooperative, user=user, role=role, member_no=member_no,
        status=Membership.Status.ACTIVE, joined_at=timezone.localdate(),
    )

    join_request.status = JoinRequest.Status.APPROVED
    join_request.decided_by = actor
    join_request.decided_at = timezone.now()
    join_request.membership = membership
    join_request.save(update_fields=["status", "decided_by", "decided_at",
                                     "membership", "updated_at"])

    from audit.services import record_action
    record_action(cooperative=cooperative, actor=actor,
                  action="membership.join_request_approved", entity=membership,
                  after={"member_no": member_no, "email": user.email})

    def _welcome():
        from communications.email import send_welcome_email
        send_welcome_email(membership)

    transaction.on_commit(_welcome)
    return membership


@transaction.atomic
def reject_join_request(join_request, *, actor, note=""):
    if join_request.status != JoinRequest.Status.PENDING:
        raise SignupError("This request has already been decided.")

    join_request.status = JoinRequest.Status.REJECTED
    join_request.decided_by = actor
    join_request.decided_at = timezone.now()
    join_request.decision_note = note
    join_request.save(update_fields=["status", "decided_by", "decided_at",
                                     "decision_note", "updated_at"])

    from audit.services import record_action
    record_action(cooperative=join_request.cooperative, actor=actor,
                  action="membership.join_request_rejected",
                  entity=join_request, after={"note": note})
    return join_request
