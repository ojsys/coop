"""Loan repayment schedules.

The cooperative uses **fixed (flat-rate) repayments** — the simplest, most
common model for a co-op: interest is a flat percentage of the amount borrowed
(``principal * rate``, a one-off charge for the loan — NOT annualised), and the
member pays the same fixed amount every month. Each instalment splits into an
equal principal share and an equal interest share; the final instalment absorbs
any rounding so the schedule sums to the total exactly. :func:`flat_schedule` /
:func:`flat_totals`.

Reducing-balance (amortized) helpers are also provided (:func:`amortize`) should
a product ever need them, but they are not used by the current loan model.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
CENTS = Decimal("0.01")


@dataclass
class Instalment:
    sequence: int
    payment: Decimal      # the fixed monthly payment (== principal + interest)
    principal: Decimal
    interest: Decimal
    balance: Decimal      # principal still owed after this payment


def flat_totals(principal, rate_percent, term) -> tuple[Decimal, Decimal]:
    """Return ``(total_interest, total_repayable)`` for a flat-rate loan.

    ``rate_percent`` is a flat percentage of the amount borrowed (e.g. ``10`` →
    10% of the principal as a one-off interest charge, independent of the term).
    """
    p = Decimal(str(principal))
    rate = Decimal(str(rate_percent)) / Decimal("100")
    total_interest = (p * rate).quantize(CENTS, rounding=ROUND_HALF_UP)
    return total_interest, p + total_interest


def flat_schedule(principal, rate_percent, term) -> list[Instalment]:
    """Return the fixed-repayment schedule for a flat-rate loan.

    Every instalment is the same fixed amount (principal share + interest share),
    with the last row absorbing rounding so principal and interest each sum
    exactly to their totals.
    """
    p = Decimal(str(principal))
    n = max(int(term or 1), 1)
    total_interest, _ = flat_totals(p, rate_percent, n)

    principal_per = (p / n).quantize(CENTS, rounding=ROUND_HALF_UP)
    interest_per = (total_interest / n).quantize(CENTS, rounding=ROUND_HALF_UP)

    rows: list[Instalment] = []
    principal_acc = ZERO
    interest_acc = ZERO
    for i in range(1, n + 1):
        if i < n:
            principal_part = principal_per
            interest_part = interest_per
        else:                       # final row clears both totals exactly
            principal_part = p - principal_acc
            interest_part = total_interest - interest_acc
        principal_acc += principal_part
        interest_acc += interest_part
        balance = (p - principal_acc).quantize(CENTS)
        rows.append(Instalment(
            sequence=i, payment=principal_part + interest_part,
            principal=principal_part, interest=interest_part,
            balance=max(balance, ZERO)))
    return rows


def emi(principal: Decimal, monthly_rate: Decimal, term: int) -> Decimal:
    """The level monthly payment for a reducing-balance loan."""
    p = Decimal(str(principal))
    n = max(int(term or 1), 1)
    r = Decimal(str(monthly_rate))
    if r == 0:
        return (p / n).quantize(CENTS, rounding=ROUND_HALF_UP)
    factor = (Decimal(1) + r) ** n
    return (p * r * factor / (factor - 1)).quantize(CENTS, rounding=ROUND_HALF_UP)


def amortize(principal, monthly_rate, term) -> list[Instalment]:
    """Return the full reducing-balance schedule.

    ``monthly_rate`` is a fraction (e.g. ``0.10`` for 10%/month). Interest each
    period is ``balance * rate``; principal is ``EMI - interest``. The last row
    clears the remaining balance exactly.
    """
    p = Decimal(str(principal))
    n = max(int(term or 1), 1)
    r = Decimal(str(monthly_rate))
    payment = emi(p, r, n)

    rows: list[Instalment] = []
    balance = p
    for i in range(1, n + 1):
        interest = (balance * r).quantize(CENTS, rounding=ROUND_HALF_UP)
        if i < n:
            principal_part = payment - interest
        else:
            # Final instalment: clear whatever principal remains.
            principal_part = balance
        this_payment = principal_part + interest
        balance = (balance - principal_part).quantize(CENTS)
        rows.append(Instalment(
            sequence=i, payment=this_payment, principal=principal_part,
            interest=interest, balance=max(balance, ZERO)))
    return rows


def totals(principal, monthly_rate, term) -> tuple[Decimal, Decimal]:
    """Return ``(total_interest, total_repayable)`` for the schedule."""
    rows = amortize(principal, monthly_rate, term)
    total_interest = sum((row.interest for row in rows), ZERO)
    total_repayable = sum((row.payment for row in rows), ZERO)
    return total_interest, total_repayable
