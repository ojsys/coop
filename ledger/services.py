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


class TransferError(LedgerError):
    """An internal transfer was refused."""


@transaction.atomic
def record_internal_transfer(*, cooperative, from_account, to_account, amount,
                            occurred_at=None, note="", recorded_by=None):
    """Record the cooperative moving its own money between two of its accounts.

    Posts one balanced journal: credit the source (money out of it), debit the
    destination (money into it). Both legs are the cooperative's own assets, so
    total holdings are unchanged and no member balance moves — which is why this
    needs one officer rather than two.

    The asset-only rule is the load-bearing guard. Without it this would accept
    Member Funds as a leg, letting an officer debit the cooperative's liability
    to its members and credit cash — a withdrawal, with none of a withdrawal's
    approval.
    """
    amount = _d(amount)
    if amount <= ZERO:
        raise TransferError("A transfer amount must be positive.")
    if from_account.pk == to_account.pk:
        raise TransferError("Choose two different accounts.")
    for account in (from_account, to_account):
        if account.cooperative_id != cooperative.id:
            raise TransferError(
                "Both accounts must belong to this cooperative.")
        if account.kind != Account.Kind.ASSET:
            raise TransferError(
                f"{account.code} {account.name} is not an asset account. "
                f"Internal transfers move money between the cooperative's own "
                f"accounts; paying money out to a member is a withdrawal, "
                f"which needs a second officer's approval.")

    available = account_balance(from_account)
    if amount > available:
        raise TransferError(
            f"{from_account.code} {from_account.name} holds "
            f"{available:,.2f}, so {amount:,.2f} cannot be moved out of it.")

    occurred_at = occurred_at or timezone.now()
    journal = post_journal(
        cooperative=cooperative,
        reference=f"XFER-{timezone.now():%y%m%d%H%M%S}",
        occurred_at=occurred_at,
        memo=note or f"Transfer {from_account.code} → {to_account.code}",
        created_by=recorded_by,
        lines=[
            Line(account=to_account, debit=amount,
                 description=f"In from {from_account.code}"),
            Line(account=from_account, credit=amount,
                 description=f"Out to {to_account.code}"),
        ],
    )

    from ledger.models import InternalTransfer

    transfer = InternalTransfer.all_objects.create(
        cooperative=cooperative,
        from_account=from_account,
        to_account=to_account,
        amount=amount,
        occurred_at=occurred_at,
        note=note,
        recorded_by=recorded_by,
        journal=journal,
    )

    from audit.services import record_action

    record_action(
        cooperative=cooperative, actor=recorded_by,
        action="ledger.internal_transfer", entity=transfer,
        after={"from": from_account.code, "to": to_account.code,
               "amount": str(amount)},
    )
    return transfer
