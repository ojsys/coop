"""
The Cooperative is the tenant root. It is *not* itself tenant-scoped — it is
the thing everything else is scoped *to*. Provisioning a cooperative seeds a
minimal chart of accounts so the double-entry ledger has somewhere to post
from day one.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


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
    # Location is country -> state -> lga. The two lower fields keep their
    # Nigerian names because renaming them would churn every console form and
    # the by_state analytics rollup for no behavioural gain; what actually
    # varies per country is the label, supplied by core.reference_data.
    country = models.CharField(
        max_length=2, blank=True,
        help_text="ISO 3166-1 alpha-2 code (e.g. NG, KE, GB). Decides what "
                  "the two fields below are called and whether they offer "
                  "dropdowns.",
    )
    state = models.CharField(
        max_length=80, blank=True,
        help_text="State / province / county — see the country's labelling.",
    )
    lga = models.CharField(
        max_length=80, blank=True,
        help_text="LGA / district / city — see the country's labelling.",
    )
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
    # Paystack's code for the bank above, needed to create the society's
    # settlement subaccount (the API takes a code, not a name). Not writable on
    # the profile for the same reason the other three are not — see
    # CooperativeUpdateSerializer.
    bank_code = models.CharField(max_length=10, blank=True)
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
            # Money the society has deposited with the platform specifically to
            # lend from. Separate from 1010 because only this balance can fund
            # an electronic disbursement: 1010 mixes money sitting in the
            # society's own bank (unreachable by a transfer) with money at the
            # PSP, so treating the two as one would let an officer try to
            # disburse funds that cannot be moved.
            ("1020", "Disbursement Wallet", Account.Kind.ASSET),
            ("2000", "Member Funds", Account.Kind.LIABILITY),
            ("4000", "Dues & Levies Income", Account.Kind.INCOME),
            ("3000", "Share Capital", Account.Kind.EQUITY),
            # The provider's fee on a wallet top-up. Without an expense account
            # the top-up journal cannot balance: the society pays the gross, the
            # platform balance receives the net, and the difference has to land
            # somewhere truthful rather than being credited to the wallet as
            # money that does not exist.
            ("5000", "Payment Charges", Account.Kind.EXPENSE),
        ]
        for code, acc_name, kind in baseline:
            Account.all_objects.get_or_create(
                cooperative=self,
                code=code,
                defaults={"name": acc_name, "kind": kind, "system": True},
            )


class BankDetailChange(TenantScopedModel, TimeStampedModel):
    """A proposed change to where a cooperative's money settles.

    Held here rather than written straight onto the Cooperative because a bank
    account is the one field where a single compromised or mistaken officer can
    redirect every future payment. It is applied only once a *different*
    privileged officer approves the matching ApprovalRequest.

    ApprovalRequest can carry only an object_id and a text summary, so the
    proposed values need a home of their own; this is it. The previous values
    are recorded too, so the approver sees what is being replaced and the audit
    trail survives the change.
    """

    bank_name = models.CharField(max_length=120, blank=True)
    bank_code = models.CharField(max_length=10, blank=True)
    bank_account_name = models.CharField(max_length=200, blank=True)
    bank_account_no = models.CharField(max_length=20, blank=True)

    # What it would replace, captured when the change is proposed.
    previous_bank_name = models.CharField(max_length=120, blank=True)
    # The code is carried through the whole proposal, not just the name. If it
    # were dropped here, applying an approved change would write the new bank
    # name over the old code — leaving a subaccount pointed at the wrong bank,
    # with nothing on screen to show it.
    previous_bank_code = models.CharField(max_length=10, blank=True)
    previous_bank_account_name = models.CharField(max_length=200, blank=True)
    previous_bank_account_no = models.CharField(max_length=20, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="proposed_bank_changes",
    )
    # Set when an approver applies it; a row with no applied_at was never used.
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (f"{self.cooperative} → {self.bank_name} "
                f"{self.bank_account_no}")

    @property
    def is_applied(self) -> bool:
        return self.applied_at is not None

    @staticmethod
    def _bank_label(name: str, code: str) -> str:
        """``Access Bank (044)`` — the code shown beside the name.

        The approver judges by the name, but the *code* is what settlement
        actually follows. Showing both means a mismatch between them is visible
        to the person approving rather than discovered when money lands at the
        wrong bank.
        """
        if name and code:
            return f"{name} ({code})"
        return name or (f"bank code {code}" if code else "")

    def describe(self) -> str:
        """Old → new, for the approver who has to judge it."""
        before = " / ".join(filter(None, [
            self._bank_label(self.previous_bank_name, self.previous_bank_code),
            self.previous_bank_account_no,
            self.previous_bank_account_name,
        ])) or "not set"
        after = " / ".join(filter(None, [
            self._bank_label(self.bank_name, self.bank_code),
            self.bank_account_no, self.bank_account_name,
        ])) or "not set"
        return f"Collection account: {before} → {after}"
