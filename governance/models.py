"""
Governance (PRD §6.4): meetings, attendance, resolutions, and tamper-evident
voting. Default is one-member-one-vote; a resolution may opt into weighting by
share capital. Votes are append-only so results are auditable.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import AppendOnlyModel, TenantScopedModel, TimeStampedModel


class Meeting(TenantScopedModel, TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        IN_PROGRESS = "in_progress", "In progress"
        CONCLUDED = "concluded", "Concluded"

    title = models.CharField(max_length=200)
    location = models.CharField(max_length=200, blank=True)
    agenda = models.TextField(blank=True)
    minutes = models.TextField(blank=True, help_text="Recorded after the meeting.")
    scheduled_at = models.DateTimeField()
    voting_opens_at = models.DateTimeField(null=True, blank=True)
    proxy_enabled = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SCHEDULED,
    )

    class Meta:
        ordering = ["-scheduled_at"]

    def __str__(self) -> str:
        return self.title


class Attendance(TenantScopedModel, TimeStampedModel):
    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="attendances",
    )
    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.CASCADE,
        related_name="attendances",
    )
    present = models.BooleanField(default=True)
    # Proxy: another member attending on this member's behalf.
    proxy_for = models.ForeignKey(
        "accounts.Membership", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="proxied_attendances",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "membership"], name="uniq_attendance",
            ),
        ]


class Resolution(TenantScopedModel, TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open for vote"
        CLOSED = "closed", "Closed"

    class VotingMode(models.TextChoices):
        ONE_MEMBER_ONE_VOTE = "one_member", "One member, one vote"
        WEIGHTED_SHARE = "weighted", "Weighted by share capital"

    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        PASSED = "passed", "Passed"
        REJECTED = "rejected", "Rejected"

    meeting = models.ForeignKey(
        Meeting, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resolutions",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.DRAFT,
    )
    voting_mode = models.CharField(
        max_length=10, choices=VotingMode.choices,
        default=VotingMode.ONE_MEMBER_ONE_VOTE,
    )
    outcome = models.CharField(
        max_length=8, choices=Outcome.choices, default=Outcome.PENDING,
    )
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="resolutions",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class Vote(AppendOnlyModel, TenantScopedModel, TimeStampedModel):
    """A single, immutable ballot. One per member per resolution."""

    class Choice(models.TextChoices):
        FOR = "for", "For"
        AGAINST = "against", "Against"
        ABSTAIN = "abstain", "Abstain"

    resolution = models.ForeignKey(
        Resolution, on_delete=models.PROTECT, related_name="votes",
    )
    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.PROTECT, related_name="votes",
    )
    choice = models.CharField(max_length=8, choices=Choice.choices)
    # Vote weight at time of casting (1 for one-member-one-vote; share capital
    # for weighted). Snapshotted so later share changes don't rewrite history.
    weight = models.DecimalField(max_digits=16, decimal_places=2, default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resolution", "membership"], name="uniq_vote_per_member",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.membership_id} → {self.choice} ({self.resolution_id})"
