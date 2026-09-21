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
