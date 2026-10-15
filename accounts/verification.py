"""
Member verification: what happens when a member's profile changes, and when
an officer decides.

Completing the profile never verifies anyone — software does not look at the
uploaded ID. It moves the member into the officers' review queue
(``awaiting``) and tells the officers; the badge comes from an officer.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from accounts.models import Membership


def member_profile_changed(membership: Membership, *, status_before: str):
    """Call after a member edits their own profile or documents.

    ``status_before`` is ``membership.verification_status`` read before the
    change.
    """
    # Declined, then the member fixed something: back into the queue, so the
    # officer's note ("ID unreadable") leads somewhere instead of being final.
    if membership.verification == Membership.Verification.REVOKED:
        membership.verification = Membership.Verification.AUTOMATIC
        membership.save(update_fields=["verification", "updated_at"])

    if (membership.verification_status == "awaiting"
            and status_before != "awaiting"):
        transaction.on_commit(lambda: _announce_ready(membership))


def _announce_ready(membership: Membership):
    from communications.models import Notification
    from communications.receipts import notify_officers_by_email
    from communications.services import notify_member

    notify_member(
        membership, kind=Notification.Kind.VERIFICATION,
        title="Your profile is complete",
        body="Thank you. An officer of your cooperative will check your "
             "details and ID, and you'll be told when you're verified.",
    )
    notify_officers_by_email(
        membership.cooperative,
        subject=f"Ready to verify: {membership.user.full_name}",
        heading="A member is ready to be verified",
        intro="They have completed their profile and uploaded a means of ID. "
              "Check the ID against their details on their member page, then "
              "verify them or decline with a note saying what to fix.",
        rows=[("Member", membership.user.full_name),
              ("Member no.", membership.member_no)],
    )


def decide(membership: Membership, *, actor, verify: bool, note: str = ""):
    """An officer verifies the member, or declines / withdraws with a note."""
    from audit.services import record_action
    from communications.models import Notification
    from communications.services import notify_member

    before = membership.verification_status
    membership.verification = (Membership.Verification.OFFICER if verify
                               else Membership.Verification.REVOKED)
    membership.verification_note = "" if verify else note[:255]
    membership.verified_by = actor
    membership.verified_at = timezone.now()
    membership.save(update_fields=["verification", "verification_note",
                                   "verified_by", "verified_at",
                                   "updated_at"])
    record_action(
        cooperative=membership.cooperative, actor=actor,
        action=("member.verified" if verify
                else "member.verification_withdrawn"),
        entity=membership,
        before={"status": before},
        after={"status": membership.verification_status,
               "note": membership.verification_note},
    )

    if verify:
        title = "You're verified"
        body = ("An officer has checked your details and ID. The verified "
                "tick now shows next to your name.")
    else:
        title = "Your verification needs attention"
        body = (f"An officer said: {membership.verification_note}\n\n"
                if membership.verification_note else "")
        body += ("Update your profile or upload a clearer ID, and it will go "
                 "back to the officers for review.")
    transaction.on_commit(lambda: notify_member(
        membership, kind=Notification.Kind.VERIFICATION,
        title=title, body=body))
    return membership
