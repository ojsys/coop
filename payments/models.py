"""
Payment provider integration & reconciliation records.

Collections settle into each cooperative's *own* PSP subaccount (Paystack /
Flutterwave). We ingest the provider's webhooks — idempotently and with
signature verification — and reconcile each settlement against an expected
contribution. Collected money never passes through the platform.

The one exception is the disbursement wallet: a float a society deliberately
deposits so it can pay loans out electronically (see WalletTopUp, Payout, and
payments.services.withdraw_wallet). That balance is the society's own money,
held only while it chooses to leave it, and withdrawable on demand.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantManager, TenantScopedModel, TimeStampedModel


class Provider(models.TextChoices):
    PAYSTACK = "paystack", "Paystack"
    FLUTTERWAVE = "flutterwave", "Flutterwave"
    # Not a payment service provider: money that reached the cooperative
    # without one — cash handed over, a bank transfer entered by an officer, or
    # a card payment confirmed through the return URL rather than a webhook.
    # Reconciliation counts PaymentEvent rows, so without a value for these the
    # whole screen stayed empty while the ledger was perfectly correct.
    INTERNAL = "manual", "Recorded directly"


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
        # Deterministic order so the paginated list is stable. Without it DRF
        # warns (UnorderedObjectListWarning) and page 2 can repeat or skip rows
        # the database happened to return in a different order.
        ordering = ["provider", "id"]
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


class WalletTopUp(TenantScopedModel, TimeStampedModel):
    """A society funding its disbursement wallet.

    Money the society deliberately deposits with the platform so it can lend
    electronically. This is the *only* money the platform holds — collections
    settle to each society's own bank through its subaccount — and it is the
    society's own, withdrawable.

    Three amounts, because they genuinely differ: ``amount`` is what the officer
    paid, ``fee`` is what the provider deducted, and ``net_amount`` is what
    actually reached the balance and therefore what the wallet is credited.
    Crediting the gross would claim money that does not exist and the first
    disbursement would fail at the provider for insufficient funds.

    Nothing is posted while PENDING: like a contribution, the ledger entry is
    written only once the charge is confirmed.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"

    amount = models.DecimalField(max_digits=16, decimal_places=2)
    fee = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    provider = models.CharField(max_length=12, choices=Provider.choices,
                                default=Provider.PAYSTACK)
    psp_reference = models.CharField(max_length=120, db_index=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="wallet_topups",
    )
    journal = models.OneToOneField(
        "ledger.Journal", on_delete=models.PROTECT, null=True, blank=True,
        related_name="wallet_topup",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One record per provider reference: a replayed confirmation or a
            # retried checkout must not credit the wallet twice.
            models.UniqueConstraint(
                fields=["provider", "psp_reference"],
                name="uniq_wallet_topup_reference",
            ),
        ]

    def __str__(self) -> str:
        return f"Wallet top-up {self.psp_reference} ({self.status})"

    @property
    def is_confirmed(self) -> bool:
        return self.status == self.Status.CONFIRMED


class Payout(TenantScopedModel, TimeStampedModel):
    """Money sent out of the platform to a bank account.

    One record per outbound transfer, whatever it funds: a loan disbursement to
    a member, a society withdrawing from its own wallet, later a savings payout.
    Linked by ``kind`` + ``object_id`` rather than a foreign key, mirroring
    ApprovalRequest — a real FK from ``payments`` into ``loans`` or ``savings``
    would invert the app dependency.

    Every payout draws on the society's disbursement wallet (ledger 1020), and
    the journal is posted when the transfer is **initiated**, not when it
    succeeds. That ordering is deliberate: if the wallet were only debited on
    success, two payouts could each pass the balance check and together
    overdraw the real balance at the provider. A failure reverses the journal
    instead (see the webhook handling).

    The destination is a snapshot. A loan's destination is already frozen at
    application, and copying it here too means the record of where money went
    survives any later edit to either the member or the loan.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"          # created, provider not yet called
        PENDING = "pending", "Sent — pending"  # provider accepted, in flight
        SUCCESS = "success", "Paid"
        FAILED = "failed", "Failed"
        REVERSED = "reversed", "Reversed"

    class Kind(models.TextChoices):
        LOAN = "loan", "Loan disbursement"
        WALLET_WITHDRAWAL = "wallet", "Wallet withdrawal"
        SAVINGS = "savings", "Savings withdrawal"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    # The loan / withdrawal this pays. Soft link: null for a wallet withdrawal,
    # which pays no particular record.
    object_id = models.PositiveIntegerField(null=True, blank=True)

    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.QUEUED)

    # Where it went, snapshotted.
    destination_bank_name = models.CharField(max_length=120, blank=True)
    destination_bank_code = models.CharField(max_length=10, blank=True)
    destination_account_no = models.CharField(max_length=20, blank=True)
    destination_account_name = models.CharField(max_length=200, blank=True)

    provider = models.CharField(max_length=12, choices=Provider.choices,
                                default=Provider.PAYSTACK)
    # Our own reference, unique per provider: this is the idempotency key the
    # provider dedupes on, and what a recheck looks the transfer up by.
    reference = models.CharField(max_length=120, db_index=True)
    recipient_code = models.CharField(max_length=120, blank=True)
    transfer_code = models.CharField(max_length=120, blank=True)

    reason = models.CharField(max_length=255, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    # Whatever the provider last told us about this transfer, for support.
    payload = models.JSONField(default=dict, blank=True)

    journal = models.OneToOneField(
        "ledger.Journal", on_delete=models.PROTECT, null=True, blank=True,
        related_name="payout",
    )
    reversal_journal = models.OneToOneField(
        "ledger.Journal", on_delete=models.PROTECT, null=True, blank=True,
        related_name="reversed_payout",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="requested_payouts",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "reference"],
                name="uniq_payout_reference",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.reference} ({self.status})"

    @property
    def is_settled(self) -> bool:
        return self.status == self.Status.SUCCESS

    @property
    def needs_recheck(self) -> bool:
        """Still in flight as far as we know.

        A provider webhook can be missed or delayed, so a payout can sit here
        while the money has in fact already left. This is what the officer's
        "recheck" action asks the provider about.
        """
        return self.status in {self.Status.QUEUED, self.Status.PENDING}

    def describe(self) -> str:
        bank = self.destination_bank_name or "unknown bank"
        if self.destination_bank_code:
            bank = f"{bank} ({self.destination_bank_code})"
        return (f"{self.amount} to {bank} · "
                f"{self.destination_account_no or 'no account number'}")


class Bank(TimeStampedModel):
    """A bank the provider can settle to or transfer to, cached from its list.

    Not tenant-scoped: the catalogue is the same for every cooperative.

    Stored in the database rather than fetched per request, and rather than held
    in Django's cache — there is no ``CACHES`` setting here, so the default is
    per-process local memory, which on this deployment means every worker
    fetches its own copy and loses it on restart. A settings form should not
    fail to render, or silently offer an empty list, because Paystack happened
    to be unreachable at that moment.

    Refreshed explicitly by ``refresh_banks`` (management command), never
    lazily on a user's request: a form render should not depend on an outbound
    HTTP call.
    """

    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, blank=True)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    provider = models.CharField(max_length=12, choices=Provider.choices,
                                default=Provider.PAYSTACK)
    # Paystack marks banks it no longer routes to. Kept rather than deleted so
    # a member whose record already holds the code still reads correctly.
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    @classmethod
    def refresh(cls, *, provider_name: str = Provider.PAYSTACK) -> dict:
        """Pull the provider's bank list into the table. Idempotent.

        Returns a small summary ``{"fetched", "created", "updated",
        "deactivated"}``. A bank that has dropped off the provider's list is
        marked inactive rather than deleted, because existing member and
        cooperative records may still reference its code.

        Returns ``fetched: 0`` and changes nothing when no live key is
        configured — the same dev short-circuit the rest of the provider uses,
        so tests and local work never reach the network.
        """
        from payments.providers import get_provider

        rows = get_provider(provider_name).list_banks()
        if not rows:
            return {"fetched": 0, "created": 0, "updated": 0,
                    "deactivated": 0}

        created = updated = 0
        seen: set[str] = set()
        for row in rows:
            code = str(row.get("code") or "").strip()
            name = (row.get("name") or "").strip()
            if not code or not name:
                continue
            seen.add(code)
            _, was_created = cls.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "slug": (row.get("slug") or "")[:120],
                    "currency": row.get("currency")
                    or settings.DEFAULT_CURRENCY,
                    "provider": provider_name,
                    "active": True,
                },
            )
            created += 1 if was_created else 0
            updated += 0 if was_created else 1

        deactivated = cls.objects.filter(
            provider=provider_name, active=True,
        ).exclude(code__in=seen).update(active=False)

        return {"fetched": len(rows), "created": created, "updated": updated,
                "deactivated": deactivated}
