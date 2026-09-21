"""
Dividend / surplus distributions. A declaration allocates a surplus pool to
members pro-rata to their share capital; posting it writes balanced journals to
the ledger (debit retained surplus, credit each member's funds).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class DividendDeclaration(TenantScopedModel, TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"

    period_label = models.CharField(max_length=40, help_text="e.g. 'FY2025'")
    total_amount = models.DecimalField(max_digits=16, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.DRAFT)
    posted_at = models.DateTimeField(null=True, blank=True)
    journal = models.ForeignKey(
        "ledger.Journal", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dividend_declarations")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="dividend_declarations")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.period_label} · {self.total_amount}"


class DividendAllocation(TenantScopedModel, TimeStampedModel):
    declaration = models.ForeignKey(
        DividendDeclaration, on_delete=models.CASCADE, related_name="allocations")
    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.CASCADE,
        related_name="dividend_allocations")
    share_capital = models.DecimalField(max_digits=14, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["-amount"]
