"""
Refer and earn: the rules.

How a referral earns:

1. A referrer shares their link (``/signup?ref=CODE``). A cooperative that
   signs up through it is credited to them, on the program's terms *as they
   are that day* — copied onto the :class:`Referral`.
2. Every invoice the cooperative pays (``platform_admin.billing.settle_invoice``)
   earns the referrer ``commission_percent`` of it, for ``commission_months``
   counted from the cooperative's first paid invoice. A free trial earns
   nothing because nothing is paid.
3. The referrer asks for their balance once it reaches the minimum. The
   platform pays it by bank transfer and marks it paid.

What is refused: referring your own cooperative (founder and referrer the same
account), a second referrer for a cooperative, a payout above the balance or
below the minimum, and a payout with no bank details to pay.

Commission must never get in the way of a payment being recorded:
:func:`credit_for_invoice` is called after the invoice is settled and its
failures are logged, not raised.
"""
from __future__ import annotations

import logging
import secrets
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from referrals.models import (Commission, Referral, ReferralPayout,
                              ReferralProgram, Referrer)

logger = logging.getLogger("api.errors")

# No 0/O or 1/I/L: codes are read aloud and typed from WhatsApp messages.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 7
CENT = Decimal("0.01")


class ReferralError(Exception):
    """A referral action that cannot be accepted as it stands."""


# ── Referrers ──────────────────────────────────────────────────────────────
def _new_code() -> str:
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))
        if not Referrer.objects.filter(code=code).exists():
            return code


def become_referrer(user) -> Referrer:
    """Make ``user`` a referrer, or return the referrer they already are."""
    existing = Referrer.objects.filter(user=user).first()
    if existing is not None:
        return existing
    if not ReferralProgram.load().active:
        raise ReferralError("The referral program is not taking new "
                            "referrers at the moment.")
    return Referrer.objects.create(user=user, code=_new_code())


@transaction.atomic
def sign_up_referrer(*, full_name, email, password, phone="") -> Referrer:
    """A person joining only to refer — no cooperative.

    An address that already has an account must give that account's
    password, for the same reason as society signup: a public form must not
    let a stranger attach things to someone else's account.
    """
    from accounts.models import User

    user = User.objects.filter(email__iexact=email).first()
    if user is not None:
        if not user.check_password(password):
            raise ReferralError(
                "That email already has an account. Use its password — or "
                "reset it from the sign-in page — and try again.")
    else:
        user = User.objects.create_user(email=email, full_name=full_name,
                                        phone=phone, password=password)
    return become_referrer(user)


def active_referrer(code: str | None) -> Referrer | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    return (Referrer.objects.select_related("user")
            .filter(code=code, status=Referrer.Status.ACTIVE).first())


# ── Referrals ──────────────────────────────────────────────────────────────
def _terms() -> dict:
    program = ReferralProgram.load()
    return {"commission_percent": program.commission_percent,
            "commission_months": program.commission_months}


def attach_at_signup(cooperative, *, code, founder) -> Referral | None:
    """Credit a cooperative signing up through a referral link.

    Quietly does nothing for an unknown, suspended or self-referring code, or
    while the program is off: a bad code must never cost a society its signup.
    """
    referrer = active_referrer(code)
    if referrer is None or not ReferralProgram.load().active:
        return None
    if referrer.user_id == founder.pk:
        return None
    referral = Referral.objects.create(referrer=referrer,
                                       cooperative=cooperative,
                                       source="signup", **_terms())
    transaction.on_commit(lambda: _tell_referrer_of_signup(referral))
    return referral


@transaction.atomic
def attach_by_platform(cooperative, *, referrer, actor) -> Referral:
    """Credit a cooperative to a referrer after it signed up — when the link
    was not used. Commissions run from invoices paid from now on."""
    if Referral.objects.filter(cooperative=cooperative).exists():
        raise ReferralError(f"{cooperative.name} is already credited to a "
                            "referrer.")
    from accounts.models import Membership

    if Membership.all_objects.filter(cooperative=cooperative,
                                     user=referrer.user).exists():
        raise ReferralError("A referrer cannot be credited with their own "
                            "cooperative.")
    referral = Referral.objects.create(referrer=referrer,
                                       cooperative=cooperative,
                                       source="platform", **_terms())
    from audit.services import record_action
    record_action(actor=actor, action="referral.attach", entity=referral,
                  after={"cooperative": cooperative.name,
                         "referrer": referrer.code})
    return referral


def window_end(referral) -> date | None:
    from platform_admin.billing import _add_months

    if referral.first_paid_on is None:
        return None
    return _add_months(referral.first_paid_on, referral.commission_months)


# ── Commission ─────────────────────────────────────────────────────────────
def credit_for_invoice(invoice) -> Commission | None:
    """Record what a paid invoice earned its cooperative's referrer.

    Safe to call more than once for the same invoice. Never raises.
    """
    try:
        return _credit(invoice)
    except Exception:  # noqa: BLE001 - must not undo a recorded payment
        logger.exception("Could not credit referral commission for invoice %s",
                         invoice.pk)
        return None


@transaction.atomic
def _credit(invoice) -> Commission | None:
    from platform_admin.models import Invoice

    if invoice.status != Invoice.Status.PAID or not invoice.amount:
        return None
    referral = (Referral.objects.select_for_update()
                .select_related("referrer__user")
                .filter(cooperative_id=invoice.cooperative_id).first())
    if referral is None or Commission.objects.filter(invoice=invoice).exists():
        return None
    # Only invoices paid after the referral was made earn on it.
    paid_on = invoice.paid_at or timezone.localdate()
    if paid_on < referral.created_at.date():
        return None

    if referral.first_paid_on is None:
        referral.first_paid_on = paid_on
        referral.save(update_fields=["first_paid_on", "updated_at"])
    if paid_on >= window_end(referral):
        return None

    amount = (invoice.amount * referral.commission_percent
              / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        return None
    commission = Commission.objects.create(
        referral=referral, invoice=invoice, invoice_amount=invoice.amount,
        percent=referral.commission_percent, amount=amount)
    transaction.on_commit(lambda: _tell_referrer_of_commission(commission))
    return commission


def void_commission(commission, *, reason, actor) -> Commission:
    """Withdraw a commission — say the payment behind it was refunded."""
    if commission.status == Commission.Status.VOID:
        return commission
    commission.status = Commission.Status.VOID
    commission.void_reason = reason
    commission.save(update_fields=["status", "void_reason", "updated_at"])
    from audit.services import record_action
    record_action(actor=actor, action="referral.commission.void",
                  entity=commission,
                  after={"amount": str(commission.amount), "reason": reason})
    return commission


# ── Money ──────────────────────────────────────────────────────────────────
def totals(referrer) -> dict[str, Decimal]:
    """Earned, paid, waiting to be paid, and available to ask for."""
    zero = Decimal("0")
    earned = (Commission.objects
              .filter(referral__referrer=referrer,
                      status=Commission.Status.EARNED)
              .aggregate(s=Sum("amount"))["s"] or zero)
    payouts = ReferralPayout.objects.filter(referrer=referrer)
    paid = (payouts.filter(status=ReferralPayout.Status.PAID)
            .aggregate(s=Sum("amount"))["s"] or zero)
    requested = (payouts.filter(status=ReferralPayout.Status.REQUESTED)
                 .aggregate(s=Sum("amount"))["s"] or zero)
    return {"earned": earned, "paid": paid, "requested": requested,
            "available": earned - paid - requested}


@transaction.atomic
def request_payout(referrer, *, amount) -> ReferralPayout:
    # Locked so two requests at once cannot both spend the same balance.
    referrer = Referrer.objects.select_for_update().get(pk=referrer.pk)
    amount = Decimal(str(amount)).quantize(CENT)
    if referrer.status != Referrer.Status.ACTIVE:
        raise ReferralError("This referral account is suspended.")
    if not referrer.has_bank_details:
        raise ReferralError("Add the bank account to pay first.")
    minimum = ReferralProgram.load().minimum_payout
    if amount < minimum:
        raise ReferralError(f"The smallest payout is ₦{minimum:,.2f}.")
    available = totals(referrer)["available"]
    if amount > available:
        raise ReferralError(f"You can ask for up to ₦{available:,.2f}.")
    payout = ReferralPayout.objects.create(
        referrer=referrer, amount=amount, bank_name=referrer.bank_name,
        bank_code=referrer.bank_code, account_no=referrer.account_no,
        account_name=referrer.account_name)
    transaction.on_commit(lambda: _tell_platform_of_payout(payout))
    return payout


def _decide(payout, *, status, actor, **fields):
    if payout.status != ReferralPayout.Status.REQUESTED:
        raise ReferralError("This payout has already been decided.")
    payout.status = status
    payout.decided_by = actor
    payout.decided_at = timezone.now()
    for key, value in fields.items():
        setattr(payout, key, value)
    payout.save()
    from audit.services import record_action
    record_action(actor=actor, action=f"referral.payout.{status}",
                  entity=payout, after={"amount": str(payout.amount),
                                        **{k: str(v) for k, v in fields.items()}})
    return payout


@transaction.atomic
def mark_payout_paid(payout, *, actor, reference="") -> ReferralPayout:
    payout = ReferralPayout.objects.select_for_update().get(pk=payout.pk)
    _decide(payout, status=ReferralPayout.Status.PAID, actor=actor,
            reference=reference.strip())
    transaction.on_commit(lambda: _tell_referrer_of_payout(payout))
    return payout


@transaction.atomic
def reject_payout(payout, *, actor, note="") -> ReferralPayout:
    """Turn a request down; the amount goes back into the balance."""
    payout = ReferralPayout.objects.select_for_update().get(pk=payout.pk)
    return _decide(payout, status=ReferralPayout.Status.REJECTED, actor=actor,
                   note=note.strip())


# ── Telling people ─────────────────────────────────────────────────────────
def _notice(user, subject, message):
    from communications.email import send_branded_email

    send_branded_email(to=user.email, subject=subject,
                       template="emails/notice.html",
                       context={"full_name": user.full_name,
                                "message": message})


def _tell_referrer_of_signup(referral):
    _notice(referral.referrer.user,
            f"{referral.cooperative.name} signed up with your link",
            f"{referral.cooperative.name} has joined through your referral "
            f"link. Once it starts paying, you earn "
            f"{referral.commission_percent.normalize()}% of each payment for "
            f"{referral.commission_months} months.")


def _tell_referrer_of_commission(commission):
    referral = commission.referral
    _notice(referral.referrer.user,
            f"You earned ₦{commission.amount:,.2f}",
            f"{referral.cooperative.name} paid an invoice of "
            f"₦{commission.invoice_amount:,.2f}, and ₦{commission.amount:,.2f} "
            f"has been added to your referral balance.")


def _tell_referrer_of_payout(payout):
    ref = f" (reference {payout.reference})" if payout.reference else ""
    _notice(payout.referrer.user,
            f"₦{payout.amount:,.2f} is on its way",
            f"We've paid ₦{payout.amount:,.2f} to {payout.bank_name} "
            f"{payout.account_no}{ref}. Thank you for spreading the word.")


def _tell_platform_of_payout(payout):
    from communications.email import platform_support_email, send_alert_email

    send_alert_email(
        to=platform_support_email(),
        subject=f"Referral payout requested: ₦{payout.amount:,.2f}",
        heading="A referrer has asked to be paid",
        rows=[("Referrer", payout.referrer.user.full_name),
              ("Amount", f"₦{payout.amount:,.2f}"),
              ("Bank", f"{payout.bank_name} · {payout.account_no}"),
              ("Account name", payout.account_name)])
