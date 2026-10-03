"""
Paying member savings back out.

Money leaving the cooperative is the most consequential thing an officer can
do here, so a withdrawal is deliberately two steps: ``request_withdrawal``
records the intent and raises an approval, and ``pay_withdrawal`` — reached only
from approvals._execute, i.e. only once a *different* privileged officer has
approved it — posts it to the ledger.

The posting debits Member Funds (2000), the liability the cooperative owes that
member, and credits whichever account the officer actually paid from. That is
the mirror of a contribution, which credits 2000 and debits cash.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

ZERO = Decimal("0.00")

# Where the money physically left from. Mirrors contributions' channel map.
_CHANNEL_ACCOUNT_CODE = {
    "cash": "1000",       # Cash
    "transfer": "1010",   # Bank / PSP Settlement
}


class WithdrawalError(Exception):
    """A withdrawal could not be requested or paid."""


def _money(value) -> str:
    # Plain-ASCII Naira so it renders in emails everywhere (as loans does).
    return f"N {Decimal(str(value)):,.2f}"


def available_balance(membership) -> Decimal:
    """What this member could withdraw right now.

    Their ledger savings balance less anything already approved-but-unpaid is
    *not* needed: a withdrawal posts the moment it is approved, so there is no
    window in which an approved payout is missing from the balance. Pending
    (unapproved) withdrawals deliberately do not reserve funds — see
    ``pay_withdrawal``, which re-checks at approval.
    """
    from ledger.services import member_balance

    return member_balance(membership)


def request_withdrawal(*, cooperative, membership, amount, channel="transfer",
                       reason="", requested_by=None):
    """Record a requested payout and send it for a second officer's approval.

    Returns ``(withdrawal, approval_request)``. Nothing is posted and no balance
    changes until the approval is granted.
    """
    from approvals.models import ApprovalRequest
    from approvals.services import submit_request
    from savings.models import Withdrawal

    amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if amount <= ZERO:
        raise WithdrawalError("A withdrawal amount must be positive.")
    if membership.cooperative_id != cooperative.id:
        raise WithdrawalError("That member does not belong to this cooperative.")
    if channel not in _CHANNEL_ACCOUNT_CODE:
        raise WithdrawalError(f"Unknown withdrawal channel: {channel!r}")

    # A transfer needs somewhere to go. Recording a payout against no account at
    # all would leave the ledger saying money left with no trace of where to.
    if channel == Withdrawal.Channel.TRANSFER and not membership.bank_account_no:
        raise WithdrawalError(
            "This member has no bank account on file. Add one to their record, "
            "or pay in cash.")

    balance = available_balance(membership)
    if amount > balance:
        raise WithdrawalError(
            f"{membership.user.full_name} holds {_money(balance)}, so "
            f"{_money(amount)} cannot be withdrawn.")

    withdrawal = Withdrawal.all_objects.create(
        cooperative=cooperative,
        membership=membership,
        amount=amount,
        channel=channel,
        reason=reason,
        destination_bank_name=membership.bank_name,
        destination_account_no=membership.bank_account_no,
        requested_by=requested_by,
    )
    approval = submit_request(
        cooperative=cooperative,
        action=ApprovalRequest.Action.SAVINGS_WITHDRAW,
        object_id=withdrawal.pk,
        requested_by=requested_by,
        note=reason,
    )
    return withdrawal, approval


@transaction.atomic
def pay_withdrawal(withdrawal, *, actor=None):
    """Post an approved withdrawal to the ledger. Idempotent.

    The balance is re-checked **here**, not only when the withdrawal was
    requested. A member's balance can fall in between — another withdrawal, a
    reversed contribution — and two pending withdrawals that were each
    affordable alone could overdraw them together. Refusing at this point is the
    difference between a rejected approval and a negative member balance.
    """
    from ledger.models import Account
    from ledger.services import Line, post_journal

    if withdrawal.is_paid:
        return withdrawal

    membership = withdrawal.membership
    coop = withdrawal.cooperative

    balance = available_balance(membership)
    if withdrawal.amount > balance:
        raise WithdrawalError(
            f"{membership.user.full_name} now holds only {_money(balance)}; "
            f"{_money(withdrawal.amount)} can no longer be paid. The balance "
            f"changed after this was requested.")

    member_funds = Account.all_objects.get(cooperative=coop, code="2000")
    paid_from = Account.all_objects.get(
        cooperative=coop, code=_CHANNEL_ACCOUNT_CODE[withdrawal.channel])

    journal = post_journal(
        cooperative=coop,
        reference=f"WDL-{withdrawal.pk}",
        memo=f"Savings withdrawal — {membership.member_no}",
        created_by=actor,
        lines=[
            # Debit the liability: the cooperative owes this member less now.
            Line(account=member_funds, debit=withdrawal.amount,
                 membership=membership,
                 description=f"Withdrawal to {membership.member_no}"),
            Line(account=paid_from, credit=withdrawal.amount,
                 description=f"{withdrawal.get_channel_display()} paid out"),
        ],
    )

    withdrawal.journal = journal
    withdrawal.paid_at = timezone.now()
    withdrawal.save(update_fields=["journal", "paid_at", "updated_at"])

    transaction.on_commit(lambda: _notify_paid(withdrawal))
    return withdrawal


def _notify_paid(withdrawal):
    """Tell the member their money went out. Never raises."""
    from communications.models import Notification
    from communications.services import notify_member

    where = (f" to {withdrawal.destination_bank_name} "
             f"{withdrawal.destination_account_no}".rstrip()
             if withdrawal.channel == "transfer" else " in cash")
    body = (
        f"{_money(withdrawal.amount)} has been withdrawn from your savings"
        f"{where}.\n\n"
        f"Your remaining balance is "
        f"{_money(available_balance(withdrawal.membership))}. If you did not "
        f"request this, contact an officer of your cooperative straight away."
    )
    notify_member(withdrawal.membership, kind=Notification.Kind.WITHDRAWAL,
                  title="Savings withdrawal paid", body=body)


def request_own_withdrawal(*, cooperative, membership, amount,
                           channel="transfer", reason=""):
    """A member asking for their own savings back.

    Thin wrapper over request_withdrawal so there is exactly one set of rules —
    the balance check, the destination check and the approval all behave
    identically however the request arrives.

    One difference worth understanding: ``requested_by`` is the *member*, so the
    maker-checker rule (a checker may not be the maker) is satisfied by any
    single officer approving. A member-initiated payout therefore carries one
    officer's authorisation, where an officer-initiated one carries two. That is
    the intended trade — the member is the beneficiary, so the officer is the
    check — but it is a weaker control and should stay a deliberate choice.
    """
    return request_withdrawal(
        cooperative=cooperative,
        membership=membership,
        amount=amount,
        channel=channel,
        reason=reason,
        requested_by=membership.user,
    )
