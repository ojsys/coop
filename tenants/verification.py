"""
A cooperative's KYC: who runs it, checked by the platform.

A society is live from the moment it signs up and keeps its own books from day
one. Verification gates only what moves money *through the platform* — the
Paystack settlement account, online collection from members, funding the
disbursement wallet and paying loans out of it. Those are what an impostor
"cooperative" could use to take money from the public, and what Paystack will
not settle without knowing who it is paying.

What a society must submit:

* a Chairperson: an active member holding the office who has uploaded a
  means of ID. Some societies are run by one person, so the Secretary and
  Treasurer are optional. When they are appointed they are listed for the
  reviewer, with their ID if they have uploaded one, but a vacancy or a
  missing ID never holds a submission back;
* the society's certificate of registration.

The checklist is computed on every read, never stored, so it cannot drift from
the documents and officers it describes. Only the outcome of a review is
stored, on ``CooperativeVerification``.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from tenants.models import CooperativeDocument, CooperativeVerification

Status = CooperativeVerification.Status

# The exco offices shown on the checklist, in display order.
EXCO_ROLES = ("chairperson", "secretary", "treasurer")
# The ones a society must fill, with an ID, before it can submit.
REQUIRED_ROLES = ("chairperson",)

REQUIRED_DOCUMENTS = (CooperativeDocument.DocType.REGISTRATION_CERTIFICATE,)
OPTIONAL_DOCUMENTS = (CooperativeDocument.DocType.BYE_LAWS,
                      CooperativeDocument.DocType.ELECTION_MINUTES)


class VerificationError(Exception):
    """A submission or review that cannot go ahead, with the reason to show."""


class VerificationRequired(PermissionDenied):
    """Raised (as HTTP 403) when a money-moving action waits on KYC."""

    default_code = "kyc_required"


def verification_for(cooperative) -> CooperativeVerification:
    verification, _ = CooperativeVerification.objects.get_or_create(
        cooperative=cooperative)
    return verification


def is_verified(cooperative) -> bool:
    return CooperativeVerification.objects.filter(
        cooperative=cooperative, status=Status.VERIFIED).exists()


def require_verified(cooperative, what: str = "Online payments") -> None:
    """Refuse ``what`` until the society's KYC has been approved."""
    if cooperative is None or is_verified(cooperative):
        return
    raise VerificationRequired(
        f"{what} open once {cooperative.name} has been verified. An officer "
        f"can finish verification from the console dashboard — it takes the "
        f"Chairperson's ID and the certificate of registration.")


# ── The checklist ───────────────────────────────────────────────────────────
def checklist(cooperative) -> dict:
    """What has been provided, what is missing, and whether it can be sent."""
    from accounts.models import MemberDocument, Membership, Role

    roles = {r.slug: r for r in Role.all_objects.filter(
        cooperative=cooperative, slug__in=EXCO_ROLES)}
    holders = (
        Membership.all_objects
        .filter(cooperative=cooperative, status=Membership.Status.ACTIVE,
                role__slug__in=EXCO_ROLES)
        .select_related("user", "role")
        .order_by("joined_at", "id")
    )
    with_id = set(
        MemberDocument.all_objects
        .filter(cooperative=cooperative, doc_type=MemberDocument.DocType.ID,
                membership__in=holders)
        .values_list("membership_id", flat=True)
    )

    officers, missing = [], []
    for slug in EXCO_ROLES:
        role = roles.get(slug)
        name = role.name if role else slug.title()
        people = [{
            "membership_id": m.id,
            "full_name": m.user.full_name,
            "email": m.user.email,
            "member_no": m.member_no,
            "user_id": m.user_id,
            "has_id": m.id in with_id,
        } for m in holders if m.role.slug == slug]
        done = any(p["has_id"] for p in people)
        required = slug in REQUIRED_ROLES
        officers.append({"role_slug": slug, "role_name": name,
                         "required": required, "holders": people,
                         "done": done})
        if not required:
            continue
        if not people:
            missing.append(f"Appoint a {name}.")
        elif not done:
            missing.append(f"The {name} must upload a means of ID.")

    uploaded = {}
    for doc in CooperativeDocument.all_objects.filter(cooperative=cooperative):
        uploaded.setdefault(doc.doc_type, []).append(doc)

    documents = []
    for doc_type in (*REQUIRED_DOCUMENTS, *OPTIONAL_DOCUMENTS):
        required = doc_type in REQUIRED_DOCUMENTS
        label = CooperativeDocument.DocType(doc_type).label
        documents.append({"doc_type": doc_type, "label": label,
                          "required": required,
                          "done": bool(uploaded.get(doc_type))})
        if required and not uploaded.get(doc_type):
            missing.append(f"Upload the {label.lower()}.")

    verification = verification_for(cooperative)
    return {
        "status": verification.status,
        "status_display": verification.get_status_display(),
        "submitted_at": verification.submitted_at,
        "reviewed_at": verification.reviewed_at,
        "reviewer_note": verification.reviewer_note,
        "officers": officers,
        "documents": documents,
        "missing": missing,
        "ready": not missing,
    }


# ── Submitting and reviewing ────────────────────────────────────────────────
@transaction.atomic
def submit(cooperative, *, actor) -> CooperativeVerification:
    verification = verification_for(cooperative)
    if verification.status == Status.VERIFIED:
        raise VerificationError(f"{cooperative.name} is already verified.")
    if verification.status == Status.SUBMITTED:
        raise VerificationError("This is already with us for review.")
    if actor.email_verified_at is None:
        raise VerificationError(
            "Confirm your email address first — we sent you a link when you "
            "signed up, and you can ask for another from the dashboard.")
    missing = checklist(cooperative)["missing"]
    if missing:
        raise VerificationError(" ".join(missing))

    before = verification.status
    verification.status = Status.SUBMITTED
    verification.submitted_at = timezone.now()
    verification.submitted_by = actor
    verification.save(update_fields=["status", "submitted_at", "submitted_by",
                                     "updated_at"])
    _audit(verification, actor, "cooperative.kyc_submitted", before)
    transaction.on_commit(lambda: _notify_submitted(verification))
    return verification


@transaction.atomic
def approve(cooperative, *, actor) -> CooperativeVerification:
    return _decide(cooperative, actor=actor, outcome=Status.VERIFIED, note="")


@transaction.atomic
def request_changes(cooperative, *, actor, note: str):
    note = (note or "").strip()
    if not note:
        raise VerificationError(
            "Say what needs changing — the officers see this note.")
    return _decide(cooperative, actor=actor,
                   outcome=Status.CHANGES_REQUESTED, note=note)


def _decide(cooperative, *, actor, outcome, note):
    verification = verification_for(cooperative)
    if verification.status != Status.SUBMITTED:
        raise VerificationError(
            "Only a submission awaiting review can be decided.")
    before = verification.status
    verification.status = outcome
    verification.reviewed_at = timezone.now()
    verification.reviewed_by = actor
    verification.reviewer_note = note[:500]
    verification.save(update_fields=["status", "reviewed_at", "reviewed_by",
                                     "reviewer_note", "updated_at"])
    action = ("cooperative.kyc_verified" if outcome == Status.VERIFIED
              else "cooperative.kyc_changes_requested")
    _audit(verification, actor, action, before)
    transaction.on_commit(lambda: _notify_decided(verification))
    return verification


def _audit(verification, actor, action, before):
    from audit.services import record_action

    record_action(
        cooperative=verification.cooperative, actor=actor, action=action,
        entity=verification.cooperative,
        before={"verification": before},
        after={"verification": verification.status,
               **({"note": verification.reviewer_note}
                  if verification.reviewer_note else {})},
    )


# ── Notifications (never allowed to undo a decision) ────────────────────────
def _notify_submitted(verification):
    from communications.email import (
        _absolute, platform_support_email, send_alert_email,
    )

    coop = verification.cooperative
    send_alert_email(
        to=platform_support_email(),
        subject=f"Verification to review: {coop.name}",
        heading="A cooperative has submitted its KYC",
        intro="Its officers' IDs and registration certificate are ready to review.",
        rows=[("Cooperative", coop.name),
              ("Submitted by", str(verification.submitted_by or "—"))],
        cta_url=_absolute("/platform/verification"),
        cta_label="Review it",
    )


def _notify_decided(verification):
    from communications.email import (
        _absolute, cooperative_notification_emails, send_alert_email,
    )

    coop = verification.cooperative
    recipients = cooperative_notification_emails(coop)
    if not recipients:
        return
    if verification.is_verified:
        send_alert_email(
            to=recipients, cooperative=coop,
            subject=f"{coop.name} is verified",
            heading="Your cooperative is verified",
            intro="Online contributions, loan repayments, the disbursement "
                  "wallet and electronic loan payouts are now open to you.",
            cta_url=_absolute("/console"), cta_label="Open the console",
        )
    else:
        send_alert_email(
            to=recipients, cooperative=coop,
            subject=f"{coop.name}: verification needs a change",
            heading="We need a change before we can verify you",
            intro=verification.reviewer_note,
            cta_url=_absolute("/console/verification"),
            cta_label="Update your verification",
        )
