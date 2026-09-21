"""
Contribution types (what members pay) and contributions (individual postings).

A ``Contribution`` is a *business-level* event; recording one always produces a
balanced journal in the ledger (see :mod:`contributions.services`). The
contribution stores a pointer to that journal so the two views — business and
accounting — stay reconciled.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class ContributionType(TenantScopedModel, TimeStampedModel):
    """A configurable thing members pay into (dues, building fund, savings)."""

    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        FLEXIBLE = "flexible", "Flexible"
        ONE_OFF = "one_off", "One-off"

    class Kind(models.TextChoices):
        MANDATORY = "mandatory", "Mandatory"
        VOLUNTARY = "voluntary", "Voluntary"
        CONDITIONAL = "conditional", "Conditional"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=60)
    frequency = models.CharField(
        max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY,
    )
    kind = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.MANDATORY,
    )
    # Null for variable amounts (e.g. target savings "any amount").
    expected_amount = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
    )
    # The account credited when a contribution of this type is recorded.
    gl_account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT,
        related_name="contribution_types",
    )
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cooperative", "slug"],
                name="uniq_contribtype_slug_per_coop",
            ),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Contribution(TenantScopedModel, TimeStampedModel):
    """A single recorded payment by a member against a contribution type."""

    class Channel(models.TextChoices):
        CASH = "cash", "Cash"
        TRANSFER = "transfer", "Bank transfer"
        PSP = "psp", "Payment provider"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        REVERSED = "reversed", "Reversed"

    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.PROTECT,
        related_name="contributions",
    )
    contribution_type = models.ForeignKey(
        ContributionType, on_delete=models.PROTECT, related_name="contributions",
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.CONFIRMED,
    )
    psp_reference = models.CharField(max_length=120, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="recorded_contributions",
    )
    occurred_at = models.DateTimeField()
    # The balanced journal this contribution posted to the ledger.
    journal = models.OneToOneField(
        "ledger.Journal", on_delete=models.PROTECT,
        null=True, blank=True, related_name="contribution",
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return f"{self.membership.member_no} {self.amount} {self.channel}"
