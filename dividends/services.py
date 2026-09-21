"""Declaring and posting dividend / surplus distributions."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


@transaction.atomic
def declare_dividend(*, cooperative, total_amount, period_label, note="",
                     created_by=None):
    """Allocate ``total_amount`` across active members pro-rata to share
    capital. Creates a DRAFT declaration + per-member allocations (no ledger
    posting yet). The largest allocation absorbs any rounding remainder so the
    allocations sum exactly to the pool."""
    from accounts.models import Membership
    from dividends.models import DividendAllocation, DividendDeclaration

    total = Decimal(str(total_amount))
    members = list(
        Membership.all_objects.filter(
            cooperative=cooperative, status=Membership.Status.ACTIVE)
        .select_related("user"))
    base = sum((m.share_capital for m in members), ZERO)

    declaration = DividendDeclaration.objects.create(
        cooperative=cooperative, period_label=period_label,
        total_amount=total, note=note, created_by=created_by)

    if base <= 0 or not members:
        return declaration  # nothing to allocate

    allocations = []
    running = ZERO
    for m in members:
        share = (total * m.share_capital / base).quantize(CENT, ROUND_HALF_UP)
        allocations.append([m, share])
        running += share
    # Push the rounding remainder onto the largest allocation.
    remainder = total - running
    if remainder != ZERO and allocations:
        allocations.sort(key=lambda a: a[1], reverse=True)
        allocations[0][1] += remainder

    DividendAllocation.objects.bulk_create([
        DividendAllocation(cooperative=cooperative, declaration=declaration,
                           membership=m, share_capital=m.share_capital,
                           amount=amount)
        for m, amount in allocations if amount > 0
    ])
    return declaration


def _surplus_account(cooperative):
    """The retained-surplus equity account dividends are drawn from."""
    from ledger.models import Account
    account, _ = Account.all_objects.get_or_create(
        cooperative=cooperative, code="3100",
        defaults={"name": "Retained Surplus", "kind": Account.Kind.EQUITY,
                  "system": True})
    return account


@transaction.atomic
def post_dividend(declaration, *, actor=None):
    """Post the declaration to the ledger: debit retained surplus, credit each
    member's Member Funds. Idempotent — a posted declaration is left alone."""
    from dividends.models import DividendAllocation, DividendDeclaration
    from ledger.models import Account
    from ledger.services import Line, post_journal

    if declaration.status == DividendDeclaration.Status.POSTED:
        return declaration

    coop = declaration.cooperative
    # Use the unscoped manager: services must not depend on ambient tenant.
    allocations = list(
        DividendAllocation.all_objects.filter(declaration=declaration)
        .select_related("membership"))
    if not allocations:
        raise ValueError("Declaration has no allocations to post.")

    surplus = _surplus_account(coop)
    member_funds = Account.all_objects.get(cooperative=coop, code="2000")
    total = sum((a.amount for a in allocations), ZERO)

    lines = [Line(account=surplus, debit=total,
                  description=f"Dividend {declaration.period_label}")]
    for a in allocations:
        lines.append(Line(account=member_funds, credit=a.amount,
                          membership=a.membership,
                          description=f"Dividend {declaration.period_label}"))

    journal = post_journal(
        cooperative=coop, reference=f"DIV-{declaration.id}", lines=lines,
        memo=f"Dividend distribution {declaration.period_label}",
        created_by=actor)

    declaration.status = DividendDeclaration.Status.POSTED
    declaration.posted_at = timezone.now()
    declaration.journal = journal
    declaration.save(update_fields=["status", "posted_at", "journal",
                                    "updated_at"])
    return declaration
