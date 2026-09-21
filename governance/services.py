"""
Resolution lifecycle + voting. Enforces eligibility, voting windows, and the
one-member-one-vote / weighted rules; tallies are derived from the immutable
Vote records.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.models import Membership
from governance.models import Resolution, Vote


class GovernanceError(Exception):
    pass


@transaction.atomic
def open_resolution(resolution, *, opens_at=None, closes_at=None, actor=None):
    """Move a resolution from draft to open for voting."""
    if resolution.status != Resolution.Status.DRAFT:
        raise GovernanceError("Only a draft resolution can be opened.")
    resolution.status = Resolution.Status.OPEN
    resolution.opens_at = opens_at or timezone.now()
    resolution.closes_at = closes_at
    resolution.save(update_fields=["status", "opens_at", "closes_at",
                                   "updated_at"])

    from audit.services import record_action
    record_action(cooperative=resolution.cooperative, actor=actor,
                  action=f'Opened resolution "{resolution.title}" for vote',
                  entity=resolution)
    return resolution


@transaction.atomic
def cast_vote(resolution, membership, choice, *, at=None):
    """Cast an eligible member's ballot on an open resolution.

    Enforces: resolution open, within window, member active & belongs to the
    same coop, and one ballot per member (unique constraint + explicit check).
    """
    now = at or timezone.now()

    if resolution.status != Resolution.Status.OPEN:
        raise GovernanceError("Resolution is not open for voting.")
    if resolution.opens_at and now < resolution.opens_at:
        raise GovernanceError("Voting has not opened yet.")
    if resolution.closes_at and now > resolution.closes_at:
        raise GovernanceError("Voting window has closed.")
    if membership.cooperative_id != resolution.cooperative_id:
        raise GovernanceError("Member is not part of this cooperative.")
    if membership.status != Membership.Status.ACTIVE:
        raise GovernanceError("Only active members may vote.")
    if choice not in Vote.Choice.values:
        raise GovernanceError(f"Invalid choice: {choice!r}")
    if Vote.all_objects.filter(resolution=resolution,
                               membership=membership).exists():
        raise GovernanceError("This member has already voted.")

    if resolution.voting_mode == Resolution.VotingMode.WEIGHTED_SHARE:
        weight = membership.share_capital or Decimal("0")
    else:
        weight = Decimal("1")

    return Vote.all_objects.create(
        cooperative=resolution.cooperative, resolution=resolution,
        membership=membership, choice=choice, weight=weight,
    )


def tally(resolution) -> dict:
    """Derive the current tally from the immutable votes."""
    result = {
        "for": {"count": 0, "weight": Decimal("0")},
        "against": {"count": 0, "weight": Decimal("0")},
        "abstain": {"count": 0, "weight": Decimal("0")},
    }
    for vote in Vote.all_objects.filter(resolution=resolution):
        bucket = result[vote.choice]
        bucket["count"] += 1
        bucket["weight"] += vote.weight

    weighted = resolution.voting_mode == Resolution.VotingMode.WEIGHTED_SHARE
    key = "weight" if weighted else "count"
    passed = result["for"][key] > result["against"][key]
    return {
        "mode": resolution.voting_mode,
        "for": result["for"],
        "against": result["against"],
        "abstain": result["abstain"],
        "total_ballots": sum(b["count"] for b in result.values()),
        "passed": passed,
    }


@transaction.atomic
def close_resolution(resolution, *, actor=None, at=None):
    """Close voting and record the tamper-evident outcome."""
    if resolution.status != Resolution.Status.OPEN:
        raise GovernanceError("Only an open resolution can be closed.")
    outcome = tally(resolution)
    resolution.status = Resolution.Status.CLOSED
    resolution.outcome = (
        Resolution.Outcome.PASSED if outcome["passed"]
        else Resolution.Outcome.REJECTED
    )
    resolution.closes_at = at or timezone.now()
    resolution.save(update_fields=["status", "outcome", "closes_at",
                                   "updated_at"])

    from audit.services import record_action
    record_action(cooperative=resolution.cooperative, actor=actor,
                  action=f'Closed resolution "{resolution.title}"',
                  entity=resolution, after={"outcome": resolution.outcome})
    return resolution
