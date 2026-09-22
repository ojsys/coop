"""
The Cooperative is the tenant root. It is *not* itself tenant-scoped — it is
the thing everything else is scoped *to*. Provisioning a cooperative seeds a
minimal chart of accounts so the double-entry ledger has somewhere to post
from day one.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class Cooperative(TimeStampedModel):
    class Status(models.TextChoices):
        PROSPECTIVE = "prospective", "Prospective"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CLOSED = "closed", "Closed"

    class Tier(models.TextChoices):
        SMALL = "small", "Small (50-500)"
        MEDIUM = "medium", "Medium (500-5,000)"
        LARGE = "large", "Large / Institutional"

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    registration_no = models.CharField(max_length=100, blank=True)
    coop_type = models.CharField(
        max_length=100, blank=True,
        help_text="e.g. multipurpose, thrift & credit, farmers",
    )
    state = models.CharField(max_length=80, blank=True)
    lga = models.CharField(max_length=80, blank=True)
    base_currency = models.CharField(
        max_length=3, default=settings.DEFAULT_CURRENCY,
    )
    tier = models.CharField(
        max_length=10, choices=Tier.choices, default=Tier.SMALL,
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PROSPECTIVE,
    )
    # Branding (design uses a green/gold theme; coops may override).
    brand_color = models.CharField(max_length=7, default="#0b4f3a")
    logo = models.ImageField(
        upload_to="coop_logos/", null=True, blank=True,
        help_text="Shown in the member app, the console header and at the top "
                  "of generated statements.",
    )
    favicon = models.ImageField(
        upload_to="coop_favicons/", null=True, blank=True,
        help_text="Square mark for the browser tab and the member PWA icon.",
    )
    member_cap = models.PositiveIntegerField(default=5000)

    # Public contact details — shown to members and printed on statements.
    # Distinct from any officer's personal details on their User record.
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_address = models.CharField(max_length=255, blank=True)
    # Replaces the default line at the foot of statements/receipts when set.
    statement_footer = models.CharField(max_length=255, blank=True)
    # The society's own collection account — shown to members who prefer to repay
    # loans (or pay dues) by direct bank transfer instead of an online gateway.
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account_name = models.CharField(max_length=200, blank=True)
    bank_account_no = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def seed_chart_of_accounts(self) -> None:
        """Create the baseline accounts a new cooperative needs to post.

        Idempotent — safe to call more than once.
        """
        from ledger.models import Account

        baseline = [
            ("1000", "Cash", Account.Kind.ASSET),
            ("1010", "Bank / PSP Settlement", Account.Kind.ASSET),
            ("2000", "Member Funds", Account.Kind.LIABILITY),
            ("4000", "Dues & Levies Income", Account.Kind.INCOME),
            ("3000", "Share Capital", Account.Kind.EQUITY),
        ]
        for code, acc_name, kind in baseline:
            Account.all_objects.get_or_create(
                cooperative=self,
                code=code,
                defaults={"name": acc_name, "kind": kind, "system": True},
            )
