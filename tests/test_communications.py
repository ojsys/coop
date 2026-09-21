"""Announcements, fan-out, opt-outs, notifications (PRD §6.6)."""
from __future__ import annotations

import pytest

from communications.models import (
    Announcement, ChannelPreference, Notification,
)
from communications.services import broadcast, notify
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _announcement(coop, channels):
    with use_tenant(coop):
        return Announcement.objects.create(
            title="AGM 2026 — date confirmed",
            body="Our AGM holds 12 July. Voting opens 09:00.",
            audience=Announcement.Audience.ALL, channels=channels,
        )


def test_broadcast_reaches_active_members_in_app(coop, make_member):
    make_member()
    make_member()
    ann = _announcement(coop, ["in_app"])
    with use_tenant(coop):
        reach = broadcast(ann)
        assert reach == 2
        assert Notification.objects.count() == 2
    ann.refresh_from_db()
    assert ann.status == Announcement.Status.SENT
    assert ann.reach == 2


def test_broadcast_respects_opt_out(coop, make_member):
    m1 = make_member()
    m2 = make_member()
    with use_tenant(coop):
        # m2 opts out; email channel selected (in-app always allowed though).
        ChannelPreference.objects.create(membership=m2, opted_out=True,
                                         email=False)
        ann = Announcement.objects.create(
            title="Email only", body="hi", channels=["email"],
        )
        reach = broadcast(ann)
    # Only m1 receives the email; m2 opted out.
    assert reach == 1


def test_cannot_send_twice(coop, make_member):
    make_member()
    ann = _announcement(coop, ["in_app"])
    with use_tenant(coop):
        broadcast(ann)
        with pytest.raises(ValueError):
            broadcast(ann)


def test_transactional_notify(coop, member):
    with use_tenant(coop):
        n = notify(member, kind=Notification.Kind.CONTRIBUTION,
                   title="Contribution received", body="+₦5,000")
        assert n.read is False
        assert Notification.objects.filter(membership=member).count() == 1
