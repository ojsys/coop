"""
Reminders for upcoming meetings.

Attendance is the thing societies most often lose to forgetfulness, and a
meeting that misses quorum cannot pass resolutions — so this is a small piece
of code with a disproportionate effect.

Run from the ``send_reminders`` management command (cPanel has no worker
process, so scheduling is a cron entry).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger("api.errors")


def upcoming_meetings(cooperative, *, days_ahead=2):
    """Scheduled meetings starting within the window, soonest first."""
    from governance.models import Meeting

    now = timezone.now()
    return (
        Meeting.all_objects
        .filter(cooperative=cooperative,
                status=Meeting.Status.SCHEDULED,
                scheduled_at__gte=now,
                scheduled_at__lte=now + timedelta(days=days_ahead))
        .order_by("scheduled_at")
    )


def send_meeting_reminders(cooperative, *, days_ahead=2, dry_run=False):
    """Remind active members about meetings happening soon.

    Returns a summary dict. Members who have opted out of a channel are
    skipped by ``notify_member``; in-app is always attempted.
    """
    from accounts.models import Membership
    from communications.models import Notification
    from communications.services import notify_member

    meetings = list(upcoming_meetings(cooperative, days_ahead=days_ahead))
    if not meetings:
        return {"meetings": 0, "reached": 0}

    members = list(
        Membership.all_objects
        .filter(cooperative=cooperative, status=Membership.Status.ACTIVE)
        .select_related("user")
    )

    reached = 0
    for meeting in meetings:
        when = timezone.localtime(meeting.scheduled_at)
        body = (f"{meeting.title} is on {when.strftime('%A %d %B')} at "
                f"{when.strftime('%H:%M')}")
        body += f", at {meeting.location}." if meeting.location else "."
        if meeting.agenda:
            body += f"\n\nAgenda:\n{meeting.agenda}"

        if dry_run:
            reached += len(members)
            continue

        for membership in members:
            notify_member(membership, kind=Notification.Kind.MEETING,
                          title=f"Reminder: {meeting.title}", body=body)
            reached += 1

    return {"meetings": len(meetings), "reached": reached}
