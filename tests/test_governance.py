"""Governance & voting integrity (PRD §6.4)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import Membership
from core.context import use_tenant
from core.models import ImmutableRecordError
from governance.models import Resolution, Vote
from governance.services import (
    GovernanceError, cast_vote, close_resolution, open_resolution, tally,
)

pytestmark = pytest.mark.django_db


def _resolution(coop, mode=Resolution.VotingMode.ONE_MEMBER_ONE_VOTE):
    with use_tenant(coop):
        return Resolution.objects.create(
            title="Increase monthly dues", voting_mode=mode,
        )


def test_cannot_vote_on_draft(coop, member):
    res = _resolution(coop)
    with use_tenant(coop):
        with pytest.raises(GovernanceError):
            cast_vote(res, member, "for")


def test_open_and_cast_one_member_one_vote(coop, make_member):
    res = _resolution(coop)
    a, b, c = make_member(), make_member(), make_member()
    with use_tenant(coop):
        open_resolution(res)
        cast_vote(res, a, "for")
        cast_vote(res, b, "for")
        cast_vote(res, c, "against")

        t = tally(res)
        assert t["for"]["count"] == 2
        assert t["against"]["count"] == 1
        assert t["passed"] is True


def test_member_cannot_vote_twice(coop, member):
    res = _resolution(coop)
    with use_tenant(coop):
        open_resolution(res)
        cast_vote(res, member, "for")
        with pytest.raises(GovernanceError):
            cast_vote(res, member, "against")


def test_inactive_member_cannot_vote(coop, make_member):
    res = _resolution(coop)
    suspended = make_member(status=Membership.Status.SUSPENDED)
    with use_tenant(coop):
        open_resolution(res)
        with pytest.raises(GovernanceError):
            cast_vote(res, suspended, "for")


def test_voting_window_enforced(coop, member):
    res = _resolution(coop)
    with use_tenant(coop):
        future = timezone.now() + timedelta(days=1)
        open_resolution(res, opens_at=future)
        with pytest.raises(GovernanceError):
            cast_vote(res, member, "for")  # window not open yet


def test_weighted_voting_by_share_capital(coop, make_member):
    res = _resolution(coop, mode=Resolution.VotingMode.WEIGHTED_SHARE)
    big = make_member(share_capital="1000000")
    small1 = make_member(share_capital="1000")
    small2 = make_member(share_capital="1000")
    with use_tenant(coop):
        open_resolution(res)
        cast_vote(res, big, "for")
        cast_vote(res, small1, "against")
        cast_vote(res, small2, "against")

        t = tally(res)
        # Two 'against' ballots but 'for' wins on share weight.
        assert t["against"]["count"] == 2
        assert t["for"]["weight"] == Decimal("1000000")
        assert t["passed"] is True


def test_close_records_outcome(coop, make_member):
    res = _resolution(coop)
    a, b = make_member(), make_member()
    with use_tenant(coop):
        open_resolution(res)
        cast_vote(res, a, "for")
        cast_vote(res, b, "for")
        close_resolution(res)
    res.refresh_from_db()
    assert res.status == Resolution.Status.CLOSED
    assert res.outcome == Resolution.Outcome.PASSED


def test_votes_are_immutable(coop, member):
    res = _resolution(coop)
    with use_tenant(coop):
        open_resolution(res)
        vote = cast_vote(res, member, "for")
    vote.choice = "against"
    with pytest.raises(ImmutableRecordError):
        vote.save()
    with pytest.raises(ImmutableRecordError):
        vote.delete()
