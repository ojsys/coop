"""
Paystack's processing fee, and passing it to the payer.

A member repaying a loan pays Paystack's fee on top of the repayment, so the
society receives the whole repayment. Paystack has no "customer pays the fee"
switch for an ordinary checkout, so the charge is grossed up: the amount sent
to checkout is the one that, after Paystack deducts its fee, leaves exactly the
repayment.

Pricing is configurable because it is Paystack's to change. The defaults are
its published local (Nigerian card) pricing:

    1.5% + ₦100, the ₦100 waived on charges under ₦2,500, capped at ₦2,000.

International cards cost more; the fee Paystack actually takes is read back
when the payment is confirmed, so any difference is booked, not guessed.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from django.conf import settings

CENT = Decimal("0.01")


def _cfg(name, default) -> Decimal:
    return Decimal(str(getattr(settings, name, default)))


def paystack_fee(charge) -> Decimal:
    """Paystack's fee on a charge of ``charge`` (local pricing)."""
    charge = Decimal(str(charge))
    rate = _cfg("PAYSTACK_FEE_PERCENT", "1.5") / 100
    flat = _cfg("PAYSTACK_FEE_FLAT", "100")
    waived_below = _cfg("PAYSTACK_FEE_FLAT_WAIVED_BELOW", "2500")
    cap = _cfg("PAYSTACK_FEE_CAP", "2000")

    fee = charge * rate + (flat if charge >= waived_below else 0)
    return min(fee, cap).quantize(CENT, ROUND_CEILING)


def gross_up(amount) -> Decimal:
    """The charge that leaves ``amount`` after Paystack's fee.

    Solved per pricing band rather than by formula alone: the flat fee switches
    on at the waiver threshold and the cap switches the percentage off, so the
    smallest charge that nets ``amount`` is found by checking each band and
    confirming the result with ``paystack_fee``.
    """
    amount = Decimal(str(amount)).quantize(CENT)
    if amount <= 0:
        return amount
    rate = _cfg("PAYSTACK_FEE_PERCENT", "1.5") / 100
    flat = _cfg("PAYSTACK_FEE_FLAT", "100")
    cap = _cfg("PAYSTACK_FEE_CAP", "2000")

    candidates = [
        amount / (1 - rate),            # under the waiver threshold
        (amount + flat) / (1 - rate),   # with the flat fee
        amount + cap,                   # capped
    ]
    for charge in sorted(c.quantize(CENT, ROUND_CEILING) for c in candidates):
        # Nudge up a kobo at a time past rounding; never more than a few.
        for _ in range(5):
            if charge - paystack_fee(charge) >= amount:
                return charge
            charge += CENT
    return (amount + cap).quantize(CENT, ROUND_CEILING)


def payer_fee(amount) -> Decimal:
    """What the payer adds on top of ``amount`` to cover Paystack's fee."""
    return gross_up(amount) - Decimal(str(amount)).quantize(CENT)
