"""
Ledger operations. All ledger writes go through here so the double-entry and
append-only invariants are enforced in exactly one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ledger.models import Account, Journal, LedgerEntry

ZERO = Decimal("0.00")


class LedgerError(Exception):
    """Base class for ledger posting errors."""


class UnbalancedJournalError(LedgerError):
    """Debits and credits do not sum to the same amount."""


@dataclass
class Line:
    """One leg of a posting. Exactly one of ``debit``/``credit`` is positive."""

    account: Account
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    membership: object | None = None
    description: str = ""
    currency: str | None = None


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@transaction.atomic
def post_journal(
    *,
    cooperative,
    reference: str,
    lines: Iterable[Line],
    occurred_at=None,
    memo: str = "",
    created_by=None,
    reversal_of: Optional[Journal] = None,
) -> Journal:
    """Post a balanced journal. Raises if it does not balance or is degenerate.

    This is the *only* supported way to write to the ledger.
    """
    lines = list(lines)
    if len(lines) < 2:
        raise LedgerError("A journal needs at least two lines (double-entry).")

    total_debit = ZERO
    total_credit = ZERO
    for ln in lines:
        debit, credit = _d(ln.debit), _d(ln.credit)
        if debit < 0 or credit < 0:
            raise LedgerError("Ledger amounts cannot be negative.")
        if (debit > 0) == (credit > 0):
            raise LedgerError(
                "Each line must be exactly one of debit or credit (non-zero)."
            )
        if ln.account.cooperative_id != cooperative.id:
            raise LedgerError(
                "Every account in a journal must belong to the cooperative."
            )
        total_debit += debit
        total_credit += credit

    if total_debit != total_credit:
        raise UnbalancedJournalError(
            f"Journal does not balance: debits {total_debit} != "
            f"credits {total_credit}."
        )

    journal = Journal(
        cooperative=cooperative,
        reference=reference,
        memo=memo,
        occurred_at=occurred_at or timezone.now(),
        created_by=created_by,
        reversal_of=reversal_of,
    )
    journal.save()

    LedgerEntry.all_objects.bulk_create([
        LedgerEntry(
            cooperative=cooperative,
            journal=journal,
            account=ln.account,
            membership=ln.membership,
            debit=_d(ln.debit),
            credit=_d(ln.credit),
            currency=ln.currency or cooperative.base_currency,
            description=ln.description,
        )
        for ln in lines
    ])
    return journal


@transaction.atomic
def reverse_journal(journal: Journal, *, created_by=None, memo: str = "") -> Journal:
    """Correct a journal by posting its mirror image (debit/credit swapped).

    The original journal is never touched — this is how the append-only ledger
    supports corrections while preserving the full audit trail.
    """
    if journal.is_reversed:
        raise LedgerError(f"Journal {journal.reference} is already reversed.")
    if journal.is_reversal:
        raise LedgerError("A reversal journal cannot itself be reversed.")

    original_entries = list(LedgerEntry.all_objects.filter(journal=journal))
    mirror = [
        Line(
            account=e.account,
            debit=e.credit,      # swap
            credit=e.debit,      # swap
            membership=e.membership,
            description=f"Reversal of {journal.reference}",
            currency=e.currency,
        )
        for e in original_entries
    ]
    return post_journal(
        cooperative=journal.cooperative,
        reference=f"REV-{journal.reference}",
        lines=mirror,
        occurred_at=timezone.now(),
        memo=memo or f"Reversal of {journal.reference}",
        created_by=created_by,
        reversal_of=journal,
    )


# --- Derived balances (never stored) --------------------------------------

def account_balance(account: Account) -> Decimal:
    """Natural-sign balance of an account, summed from the ledger.

    Derived from an explicit account (which already identifies its cooperative),
    so this is independent of any ambient tenant context.
    """
    agg = LedgerEntry.all_objects.filter(account=account).aggregate(
        d=Sum("debit"), c=Sum("credit"),
    )
    debit = agg["d"] or ZERO
    credit = agg["c"] or ZERO
    return (debit - credit) if account.is_debit_normal else (credit - debit)


def member_balance(membership) -> Decimal:
    """Total member funds held for a member (a liability the coop owes them)."""
    agg = LedgerEntry.all_objects.filter(
        membership=membership,
        account__kind=Account.Kind.LIABILITY,
    ).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["c"] or ZERO) - (agg["d"] or ZERO)


def member_statement(membership):
    """Return the member's ledger lines with a running balance, oldest first.

    Yields dicts suitable for a statement (screen or PDF).
    """
    entries = (
        LedgerEntry.all_objects.filter(
            membership=membership,
            account__kind=Account.Kind.LIABILITY,
        )
        .select_related("journal", "account")
        .order_by("journal__occurred_at", "id")
    )
    running = ZERO
    rows = []
    for e in entries:
        running += e.credit - e.debit
        rows.append({
            "date": e.journal.occurred_at,
            "reference": e.journal.reference,
            "description": e.description or e.journal.memo,
            "credit": e.credit,
            "debit": e.debit,
            "balance": running,
        })
    return rows
