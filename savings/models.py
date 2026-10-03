"""
Savings products (named plans a cooperative offers, e.g. Ajo / target savings)
and per-member goals with ledger-derived progress.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class SavingsProduct(TenantScopedModel, TimeStampedModel):
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    interest_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Indicative annual interest rate (%).")
    # Optionally the contribution type whose confirmed payments count toward a
    # goal on this product; when unset, progress uses the member's savings.
    contribution_type = models.ForeignKey(
        "contributions.ContributionType", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="savings_products")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SavingsGoal(TenantScopedModel, TimeStampedModel):
    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.CASCADE,
        related_name="savings_goals")
    product = models.ForeignKey(
        SavingsProduct, on_delete=models.PROTECT, related_name="goals")
    name = models.CharField(max_length=120)
    target_amount = models.DecimalField(max_digits=14, decimal_places=2)
    target_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.membership.member_no})"

    @property
    def saved_amount(self) -> Decimal:
        """Progress toward the goal, derived from the ledger.

        If the product is tied to a contribution type, sum the member's
        confirmed contributions of that type; otherwise use their savings.
        """
        from contributions.models import Contribution

        if self.product.contribution_type_id:
            agg = (
                Contribution.all_objects
                .filter(cooperative_id=self.cooperative_id,
                        membership=self.membership,
                        contribution_type_id=self.product.contribution_type_id,
                        status=Contribution.Status.CONFIRMED)
                .aggregate(s=models.Sum("amount"))
            )
            return agg["s"] or Decimal("0.00")
        return self.membership.savings_balance


class Withdrawal(TenantScopedModel, TimeStampedModel):
    """A member being paid out some of their savings.

    Nothing moves when this is created. A withdrawal is money leaving the
    cooperative, so it is recorded here, approved by a *different* privileged
    officer through the approvals queue, and only then posted to the ledger —
    debiting Member Funds (the liability owed to that member) and crediting
    whichever account the officer actually paid from.

    The destination bank details are **copied** rather than referenced: a payout
    record has to say where the money went, and a member who edits their account
    afterwards must not rewrite the history of a payment already made.

    There is no "cancel" once paid. The ledger is append-only, so a mistaken
    payout is corrected by a reversing journal, with the original left visible.
    """

    class Channel(models.TextChoices):
        CASH = "cash", "Cash"
        TRANSFER = "transfer", "Bank transfer"

    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.PROTECT,
        related_name="withdrawals")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    channel = models.CharField(max_length=10, choices=Channel.choices,
                               default=Channel.TRANSFER)
    reason = models.CharField(max_length=255, blank=True)

    # Where it was sent, as it stood when the withdrawal was requested.
    destination_bank_name = models.CharField(max_length=120, blank=True)
    destination_account_no = models.CharField(max_length=20, blank=True)

    requested_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="requested_withdrawals")
    paid_at = models.DateTimeField(null=True, blank=True)
    journal = models.OneToOneField(
        "ledger.Journal", on_delete=models.PROTECT, null=True, blank=True,
        related_name="withdrawal",
        help_text="The ledger posting, set when the payout is approved.")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.membership.member_no} · {self.amount}"

    @property
    def is_paid(self) -> bool:
        return self.paid_at is not None

    def describe(self) -> str:
        """Who, how much, and to where — this is the only text the approver sees."""
        where = (f"{self.destination_bank_name} {self.destination_account_no}".strip()
                 if self.channel == self.Channel.TRANSFER else "cash")
        return (f"Pay {self.membership.user.full_name} "
                f"({self.membership.member_no}) N {self.amount:,.2f} "
                f"to {where or 'no account on file'}")
