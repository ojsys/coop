"""
Announcement fan-out + transactional notifications.

Channel delivery is abstracted so providers can be swapped (PRD §7): in-app is
always a Notification row; email uses Django's mail backend (console in dev,
Brevo SMTP in prod); SMS/WhatsApp are stubbed behind :func:`_send_sms` /
:func:`_send_whatsapp` pending Termii/Africa's Talking wiring.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import Membership
from communications.models import Announcement, ChannelPreference, Notification

logger = logging.getLogger(__name__)


def _preference_for(membership) -> ChannelPreference:
    pref = getattr(membership, "channel_preference", None)
    if pref is not None:
        return pref
    # Default: everything on.
    return ChannelPreference(membership=membership)


def _audience(announcement) -> list:
    qs = Membership.all_objects.filter(
        cooperative=announcement.cooperative,
        status=Membership.Status.ACTIVE,
    ).select_related("user")
    if announcement.audience == Announcement.Audience.ROLE and \
            announcement.audience_role_id:
        qs = qs.filter(role_id=announcement.audience_role_id)
    return list(qs)


def _send_email(member, subject, body) -> bool:
    """Deliver one notification, branded as the member's own society.

    Failures are logged rather than swallowed. The previous implementation
    passed ``fail_silently=True``, which made a wrong SMTP password look
    exactly like working mail.
    """
    from communications.email import send_branded_email

    email = member.user.email
    if not email:
        return False
    return send_branded_email(
        to=email,
        subject=subject,
        template="emails/notice.html",
        cooperative=member.cooperative,
        context={"full_name": member.user.full_name, "message": body},
    )


def _send_sms(member, body) -> bool:  # pragma: no cover - provider stub
    logger.info("SMS to %s: %s", member.user.phone, body[:60])
    return bool(member.user.phone)


def _send_whatsapp(member, body) -> bool:  # pragma: no cover - provider stub
    logger.info("WhatsApp to %s: %s", member.user.phone, body[:60])
    return bool(member.user.phone)


@transaction.atomic
def broadcast(announcement) -> int:
    """Fan an announcement out to its audience across selected channels.

    Returns the reach (number of members who received it on at least one
    channel). Opt-outs are respected; in-app is always attempted.
    """
    if announcement.status == Announcement.Status.SENT:
        raise ValueError("Announcement has already been sent.")

    channels = set(announcement.channels or ["in_app"])
    reached = 0
    for member in _audience(announcement):
        pref = _preference_for(member)
        delivered = False

        if "in_app" in channels and pref.allows("in_app"):
            Notification.all_objects.create(
                cooperative=announcement.cooperative, membership=member,
                kind=Notification.Kind.ANNOUNCEMENT, title=announcement.title,
                body=announcement.body, announcement=announcement,
            )
            delivered = True
        if "email" in channels and pref.allows("email"):
            delivered = _send_email(member, announcement.title,
                                    announcement.body) or delivered
        if "sms" in channels and pref.allows("sms"):
            delivered = _send_sms(member, announcement.body) or delivered
        if "whatsapp" in channels and pref.allows("whatsapp"):
            delivered = _send_whatsapp(member, announcement.body) or delivered

        if delivered:
            reached += 1

    announcement.status = Announcement.Status.SENT
    announcement.reach = reached
    announcement.sent_at = timezone.now()
    announcement.save(update_fields=["status", "reach", "sent_at",
                                     "updated_at"])

    from audit.services import record_action
    record_action(cooperative=announcement.cooperative,
                  actor=announcement.created_by,
                  action=f'Broadcast announcement "{announcement.title}"',
                  entity=announcement, after={"reach": reached})
    return reached


def notify(membership, *, kind, title, body="") -> Notification:
    """Create a transactional in-app notification for one member."""
    return Notification.all_objects.create(
        cooperative=membership.cooperative, membership=membership,
        kind=kind, title=title, body=body,
    )


def notify_member(membership, *, kind, title, body="", email=True) -> Notification:
    """Transactional notification across in-app **and** email.

    In-app is always written (subject to the member's in-app preference); email
    is attempted when ``email`` is set and the member hasn't opted out of it.
    Returns the in-app :class:`Notification`.
    """
    pref = _preference_for(membership)
    notification = None
    if pref.allows("in_app"):
        notification = notify(membership, kind=kind, title=title, body=body)
    if email and pref.allows("email"):
        _send_email(membership, title, body)
    return notification
