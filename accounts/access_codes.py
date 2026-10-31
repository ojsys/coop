"""
Access codes: how a member with no email sets or resets their password.

An officer of the society issues one from the member's record and hands it
over in person or by phone. The member types it with their phone number in the
app or on the website and chooses a password. No SMS provider is involved; when
one is wired up, "text me a code" can issue the same codes.

A code is eight digits, shown to the officer once and stored hashed. It works
once, for 72 hours, and is burnt after five wrong tries. Issuing a new one
cancels any earlier code for that person.

Officers manage their own society's people, not the platform's: a code cannot
be issued for someone who holds an office in another society, staff the
platform or refer societies, since setting their password would hand over
more than this society's affairs. Only the Chairperson issues one for another
officer of the society.

Imported from ``accounts/models.py`` so Django discovers the model.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import models, transaction
from django.utils import timezone

from core.models import TimeStampedModel

CODE_DIGITS = 8
LIFETIME = timedelta(hours=72)
MAX_ATTEMPTS = 5


class AccessCode(TimeStampedModel):
    """A one-time code that lets a person choose a new password."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE,
                             related_name="access_codes")
    code_hash = models.CharField(max_length=128)
    # The society whose officer issued it, for the audit trail and for the
    # message the member sees.
    cooperative = models.ForeignKey("tenants.Cooperative",
                                    on_delete=models.CASCADE,
                                    related_name="+")
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL,
                                  on_delete=models.SET_NULL, null=True,
                                  related_name="+")
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_live(self) -> bool:
        return (self.used_at is None and self.attempts < MAX_ATTEMPTS
                and self.expires_at > timezone.now())


class AccessCodeError(Exception):
    """The code cannot be issued, or does not work."""


def officer_may_issue(actor, membership) -> str | None:
    """Why ``actor`` may not issue a code for ``membership``, or ``None``."""
    from accounts.models import Membership, Role

    user = membership.user
    if user.pk == actor.pk:
        return "Change your own password from your profile."
    if not user.is_active or membership.status != Membership.Status.ACTIVE:
        return "Only an active member can be given an access code."
    if user.is_platform_admin or user.is_staff:
        return "This person's account is managed by the platform."

    from referrals.models import Referrer
    if Referrer.objects.filter(user=user).exists():
        return ("This person refers societies to CooperativeOS, so only they "
                "can reset their password.")

    office_elsewhere = Membership.all_objects.filter(
        user=user, status=Membership.Status.ACTIVE,
    ).exclude(cooperative_id=membership.cooperative_id).select_related("role")
    if any(m.role and m.role.is_officer for m in office_elsewhere):
        return ("This person is an officer of another society, so only they "
                "can reset their password.")

    if membership.role and membership.role.is_officer and not actor.is_platform_admin:
        mine = Membership.all_objects.filter(
            user=actor, cooperative_id=membership.cooperative_id,
        ).select_related("role").first()
        if not (mine and mine.role and mine.role.slug == Role.CHAIRPERSON):
            return "Only the Chairperson can give another officer an access code."
    return None


@transaction.atomic
def issue_access_code(membership, *, actor) -> tuple[AccessCode, str]:
    """A new code for this member. Returns the row and the code itself, which
    is never stored and cannot be shown again."""
    reason = officer_may_issue(actor, membership)
    if reason:
        raise AccessCodeError(reason)

    user = membership.user
    now = timezone.now()
    # One live code at a time: the newest is the one the officer just read out.
    AccessCode.objects.filter(user=user, used_at=None).update(used_at=now)
    code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
    row = AccessCode.objects.create(
        user=user, code_hash=make_password(code),
        cooperative=membership.cooperative, issued_by=actor,
        expires_at=now + LIFETIME)

    from audit.services import record_action
    record_action(cooperative=membership.cooperative, actor=actor,
                  action="member.access_code_issued", entity=membership,
                  after={"expires_at": row.expires_at.isoformat()})

    if user.email:
        def _tell():
            from communications.email import send_access_code_notice
            send_access_code_notice(row)
        transaction.on_commit(_tell)
    return row, code


def _clean(code) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def redeem_access_code(*, user, code, password):
    """Set ``user``'s password if ``code`` is their live code.

    Raises AccessCodeError for a wrong, used or expired code; every wrong try
    counts towards burning it, so the count is committed before raising. The
    caller checks the password's strength.
    """
    code = _clean(code)
    with transaction.atomic():
        live = [row for row in (AccessCode.objects.select_for_update()
                                .filter(user=user, used_at=None))
                if row.is_live]
        match = next((row for row in live
                      if code and check_password(code, row.code_hash)), None)
        if match is None:
            AccessCode.objects.filter(pk__in=[r.pk for r in live]).update(
                attempts=models.F("attempts") + 1)
        else:
            match.used_at = timezone.now()
            match.save(update_fields=["used_at", "updated_at"])
            user.set_password(password)
            user.save(update_fields=["password", "updated_at"])

            from rest_framework.authtoken.models import Token
            # Whoever was signed in as them before is signed out.
            Token.objects.filter(user=user).delete()

            from audit.services import record_action
            record_action(cooperative=match.cooperative, actor=user,
                          action="account.password_set_with_access_code",
                          entity=user)
    if match is None:
        raise AccessCodeError("That code is not right, or it has expired. "
                              "Ask your society's secretary for a new one.")
    return user
