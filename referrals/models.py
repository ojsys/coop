"""
Refer and earn: people who bring cooperatives to the platform earn a share of
what those cooperatives pay.

* :class:`ReferralProgram` — the terms (a singleton the platform edits).
* :class:`Referrer` — an account that refers. Any account can be one: a
  referrer signed up for nothing else, or a member or officer of a society.
* :class:`Referral` — a cooperative brought in by a referrer. A cooperative has
  at most one. The terms are copied onto it when it signs up, so changing the
  program never changes what an existing referral earns.
* :class:`Commission` — what one paid invoice earned. One per invoice.
* :class:`ReferralPayout` — a referrer asking for their balance. The platform
  pays it by bank transfer, outside the system, and marks it paid.

See ``referrals.services``.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class ReferralProgram(TimeStampedModel):
    """The terms new referrals are made on."""

    active = models.BooleanField(
        default=True,
        help_text="Off: no new referrers, and new signups are not credited "
                  "to anyone. Existing referrals keep earning on their terms.")
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10.00"),
        help_text="Share of each paid invoice the referrer earns.")
    commission_months = models.PositiveIntegerField(
        default=12,
        help_text="How long a referral earns, counted from the cooperative's "
                  "first paid invoice.")
    minimum_payout = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("5000.00"),
        help_text="The smallest balance a referrer can ask to be paid.")

    @classmethod
    def load(cls) -> "ReferralProgram":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return (f"{self.commission_percent}% for {self.commission_months} "
                f"months")


class Referrer(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    user = models.OneToOneField(settings.AUTH_USER_MODEL,
                                on_delete=models.CASCADE,
                                related_name="referrer")
    code = models.CharField(max_length=16, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.ACTIVE)
    # Where payouts go. A referrer may have no cooperative, so these live
    # here rather than on a membership.
    bank_name = models.CharField(max_length=120, blank=True)
    bank_code = models.CharField(max_length=10, blank=True)
    account_no = models.CharField(max_length=20, blank=True)
    account_name = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user.full_name} ({self.code})"

    @property
    def has_bank_details(self) -> bool:
        return bool(self.bank_name and self.account_no and self.account_name)


class Referral(TimeStampedModel):
    referrer = models.ForeignKey(Referrer, on_delete=models.PROTECT,
                                 related_name="referrals")
    cooperative = models.OneToOneField(
        "tenants.Cooperative", on_delete=models.CASCADE,
        related_name="referral")
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission_months = models.PositiveIntegerField()
    # Set by the first paid invoice; the earning window runs from here.
    first_paid_on = models.DateField(null=True, blank=True)
    # How it was made: from a referral link at signup, or attached by the
    # platform afterwards.
    source = models.CharField(max_length=10, default="signup")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.cooperative} ← {self.referrer}"


class Commission(TimeStampedModel):
    class Status(models.TextChoices):
        EARNED = "earned", "Earned"
        VOID = "void", "Void"

    referral = models.ForeignKey(Referral, on_delete=models.PROTECT,
                                 related_name="commissions")
    invoice = models.OneToOneField("platform_admin.Invoice",
                                   on_delete=models.PROTECT,
                                   related_name="commission")
    invoice_amount = models.DecimalField(max_digits=12, decimal_places=2)
    percent = models.DecimalField(max_digits=5, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=6, choices=Status.choices,
                              default=Status.EARNED)
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"₦{self.amount} on {self.invoice}"


class ReferralPayout(TimeStampedModel):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Rejected"

    referrer = models.ForeignKey(Referrer, on_delete=models.PROTECT,
                                 related_name="payouts")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    # Snapshot of where to pay, as it was when asked: editing bank details
    # afterwards must not redirect a payout already in the platform's queue.
    bank_name = models.CharField(max_length=120)
    bank_code = models.CharField(max_length=10, blank=True)
    account_no = models.CharField(max_length=20)
    account_name = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.REQUESTED)
    reference = models.CharField(
        max_length=120, blank=True,
        help_text="The bank transfer's reference, recorded when paid.")
    note = models.CharField(max_length=255, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"₦{self.amount} to {self.referrer}"
