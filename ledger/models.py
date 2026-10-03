"""
The double-entry, append-only ledger — the financial system of record.

Design rules (PRD §8 "Key architectural decisions"), enforced here:

* **Append-only.** A ``Journal`` and its ``LedgerEntry`` lines are immutable
  once written. ``save`` on an existing row and ``delete`` both raise. There is
  no in-place balance edit anywhere in the product.
* **Double-entry.** Every journal balances: total debits equal total credits
  (enforced by :func:`ledger.services.post_journal`).
* **Corrections by reversal.** A mistake is fixed by posting a *new* journal
  whose lines mirror the original with debit/credit swapped, linked via
  ``reversal_of``. The original always remains visible for audit.
* **Derived balances.** No account or member row stores a mutable balance;
  balances are always summed from the ledger.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class ImmutableLedgerError(Exception):
    """Raised on any attempt to mutate or delete a posted ledger record."""


class AppendOnlyModel(models.Model):
    """Mixin: allow insert, forbid update and delete."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableLedgerError(
                f"{type(self).__name__} is append-only and cannot be modified "
                "once posted. Post a reversing entry instead."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableLedgerError(
            f"{type(self).__name__} is append-only and cannot be deleted. "
            "Post a reversing entry instead."
        )


class Account(TenantScopedModel, TimeStampedModel):
    """A chart-of-accounts line for one cooperative."""

    class Kind(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    # Accounts whose natural (increasing) side is debit.
    DEBIT_NORMAL = {Kind.ASSET, Kind.EXPENSE}

    code = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    # True for accounts seeded at provisioning; protected from deletion.
    system = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cooperative", "code"], name="uniq_account_code_per_coop",
            ),
        ]
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @property
    def is_debit_normal(self) -> bool:
        return self.kind in self.DEBIT_NORMAL

    @property
    def balance(self):
        from ledger.services import account_balance

        return account_balance(self)


class Journal(AppendOnlyModel, TenantScopedModel, TimeStampedModel):
    """A balanced posting (transaction header) grouping its ledger lines."""

    reference = models.CharField(max_length=60)
    memo = models.CharField(max_length=255, blank=True)
    occurred_at = models.DateTimeField(
        help_text="When the underlying event happened (business date).",
    )
    posted_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="journals",
    )
    # If set, this journal reverses another one (a correction).
    reversal_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="reversed_by",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cooperative", "reference"],
                name="uniq_journal_ref_per_coop",
            ),
        ]
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return self.reference

    @property
    def is_reversal(self) -> bool:
        return self.reversal_of_id is not None

    @property
    def is_reversed(self) -> bool:
        return hasattr(self, "reversed_by")


class LedgerEntry(AppendOnlyModel, TenantScopedModel, TimeStampedModel):
    """A single debit-or-credit line within a journal."""

    journal = models.ForeignKey(
        Journal, on_delete=models.PROTECT, related_name="entries",
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="entries",
    )
    # Optional member dimension — lets us derive per-member balances.
    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.PROTECT,
        null=True, blank=True, related_name="ledger_entries",
    )
    debit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default=settings.DEFAULT_CURRENCY)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            # Amounts are never negative...
            models.CheckConstraint(
                condition=models.Q(debit__gte=0) & models.Q(credit__gte=0),
                name="ledger_amounts_non_negative",
            ),
            # ...and a line is exactly one of debit XOR credit.
            models.CheckConstraint(
                condition=(
                    (models.Q(debit__gt=0) & models.Q(credit=0))
                    | (models.Q(debit=0) & models.Q(credit__gt=0))
                ),
                name="ledger_debit_xor_credit",
            ),
        ]
        ordering = ["id"]

    def __str__(self) -> str:
        side = f"Dr {self.debit}" if self.debit else f"Cr {self.credit}"
        return f"{self.account.code} {side}"


class InternalTransfer(TenantScopedModel, TimeStampedModel):
    """A record of the cooperative moving its own money between its accounts.

    Fund movement is not automatic: an officer makes the real transfer at the
    bank, then records it here so the ledger matches reality — typically
    sweeping collections out of Bank/PSP Settlement into Cash, or the reverse.

    Deliberately **not** approval-gated, unlike a member withdrawal. Nothing
    leaves the cooperative: both legs are its own asset accounts, the totals net
    to zero, and no member's balance moves. That is the same risk profile as
    recording a contribution, which one officer already posts directly. The
    control that matters here is in the service, which refuses any account that
    is not an asset — otherwise "internal transfer" would be a way to debit
    Member Funds and pay money out without a second officer.
    """

    from_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="transfers_out")
    to_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="transfers_in")
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    # When the money actually moved at the bank, which may not be when an
    # officer got round to recording it.
    occurred_at = models.DateTimeField()
    note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="internal_transfers")
    journal = models.OneToOneField(
        Journal, on_delete=models.PROTECT, related_name="internal_transfer",
        help_text="The balanced posting this transfer produced.")

    class Meta:
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return (f"{self.amount} {self.from_account.code}→"
                f"{self.to_account.code}")

    def describe(self) -> str:
        return (f"{self.from_account.code} {self.from_account.name} → "
                f"{self.to_account.code} {self.to_account.name}")
