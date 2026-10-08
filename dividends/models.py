"""
Dividend / surplus distributions.

A declaration allocates a surplus pool to members pro-rata to their share
capital — either a fixed pool, or a rate on share capital ("10% on shares").
It starts as a DRAFT, is posted only once a *second* officer approves it
(approvals.ApprovalRequest, action ``dividend.post``), and a posted one is
corrected by reversal, never by deletion:

    DRAFT ──approve──▶ POSTED ──reverse──▶ REVERSED

Posting writes one balanced journal: debit 3100 Retained Surplus, credit each
member's funds (2000), so the dividend lands in the member's savings.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class DividendDeclaration(TenantScopedModel, TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        REVERSED = "reversed", "Reversed"

    class Method(models.TextChoices):
        POOL = "pool", "Fixed pool shared pro-rata"
        RATE = "rate", "Rate on share capital"

    period_label = models.CharField(max_length=40, help_text="e.g. 'FY2025'")
    method = models.CharField(max_length=4, choices=Method.choices,
                              default=Method.POOL)
    # Only for RATE: the % of each member's share capital paid out.
    rate = models.DecimalField(max_digits=6, decimal_places=2, null=True,
                               blank=True)
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
    reversal_journal = models.ForeignKey(
        "ledger.Journal", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reversed_dividend_declarations")
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="reversed_dividends")
    reversal_reason = models.CharField(max_length=255, blank=True)

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
