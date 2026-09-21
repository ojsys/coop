"""
Payment provider integration & reconciliation records.

Money settles into each cooperative's *own* PSP subaccount (Paystack /
Flutterwave). We ingest the provider's webhooks — idempotently and with
signature verification — and reconcile each settlement against an expected
contribution. We never hold funds.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantManager, TenantScopedModel, TimeStampedModel


class Provider(models.TextChoices):
    PAYSTACK = "paystack", "Paystack"
    FLUTTERWAVE = "flutterwave", "Flutterwave"


class ProviderAccount(TenantScopedModel, TimeStampedModel):
    """A cooperative's own settlement subaccount with a PSP.

    The subaccount code is how an inbound webhook is routed back to the right
    cooperative (tenant).
    """

    provider = models.CharField(max_length=12, choices=Provider.choices)
    subaccount_code = models.CharField(max_length=120)
    bank_name = models.CharField(max_length=120, blank=True)
    connected = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "subaccount_code"],
                name="uniq_provider_subaccount",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_display()} {self.subaccount_code}"


class PaymentEvent(TimeStampedModel):
    """A raw PSP webhook, deduplicated by (provider, event_id).

    Unlike other tenant-owned models, ``cooperative`` is *nullable* here: a
    webhook whose settlement subaccount we don't recognise can't be routed to a
    tenant, but we still persist it (as an exception) for platform visibility.
    Reads through ``objects`` are tenant-scoped; ``all_objects`` is unscoped.

    ``status`` records the reconciliation outcome so exceptions can be surfaced
    for manual resolution (PRD §6.3).
    """

    cooperative = models.ForeignKey(
        "tenants.Cooperative", on_delete=models.CASCADE,
        null=True, blank=True, related_name="payment_events", db_index=True,
    )
    objects = TenantManager()
    all_objects = models.Manager()

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"        # ingested, not yet matched
        MATCHED = "matched", "Matched"           # settled a contribution
        UNMATCHED = "unmatched", "Unmatched"     # no expected contribution
        PARTIAL = "partial", "Partial / amount mismatch"
        DUPLICATE = "duplicate", "Duplicate settlement"
        IGNORED = "ignored", "Ignored"           # e.g. non-success event

    provider = models.CharField(max_length=12, choices=Provider.choices)
    event_id = models.CharField(max_length=160)
    reference = models.CharField(max_length=160, db_index=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.RECEIVED,
    )
    matched_contribution = models.ForeignKey(
        "contributions.Contribution", on_delete=models.PROTECT,
        null=True, blank=True, related_name="payment_events",
    )
    note = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Idempotency: a provider never processes the same event twice.
            models.UniqueConstraint(
                fields=["provider", "event_id"],
                name="uniq_payment_event",
            ),
        ]
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        return f"{self.provider}:{self.reference} ({self.status})"

    @property
    def is_exception(self) -> bool:
        return self.status in {
            self.Status.UNMATCHED, self.Status.PARTIAL, self.Status.DUPLICATE,
        }
