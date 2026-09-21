"""
Recording and reversing contributions. Each contribution posts a balanced
double-entry journal:

    Dr  <settlement asset>   (Cash, or Bank/PSP)     amount
        Cr  <type GL account>  (Member Funds, ...)   amount   [member dimension]

Money lands in the cooperative's *own* settlement account — the platform never
holds funds (PRD "ledger, not custodian").
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ledger.models import Account
from ledger.services import Line, post_journal, reverse_journal


class ContributionError(Exception):
    pass


# Which asset account each channel settles into.
_CHANNEL_ACCOUNT_CODE = {
    "cash": "1000",       # Cash
    "transfer": "1010",   # Bank / PSP Settlement
    "psp": "1010",
}


def _settlement_account(cooperative, channel: str) -> Account:
    code = _CHANNEL_ACCOUNT_CODE.get(channel)
    if code is None:
        raise ContributionError(f"Unknown contribution channel: {channel!r}")
    try:
        # Explicit cooperative → tenant-context-independent (also correct when
        # called from a webhook, which has no ambient tenant bound).
        return Account.all_objects.get(cooperative=cooperative, code=code)
    except Account.DoesNotExist as exc:  # pragma: no cover - provisioning bug
        raise ContributionError(
            "Settlement account missing — was the chart of accounts seeded?"
        ) from exc


def _reference() -> str:
    return f"CTB-{timezone.now():%y%m%d}-{uuid.uuid4().hex[:8].upper()}"


@transaction.atomic
def record_contribution(
    *,
    cooperative,
    membership,
    contribution_type,
    amount,
    channel: str,
    occurred_at=None,
    psp_reference: str = "",
    recorded_by=None,
):
    """Record a member contribution and post it to the ledger atomically."""
    from contributions.models import Contribution

    amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if amount <= 0:
        raise ContributionError("Contribution amount must be positive.")
    if membership.cooperative_id != cooperative.id:
        raise ContributionError("Member does not belong to this cooperative.")
    if contribution_type.cooperative_id != cooperative.id:
        raise ContributionError("Contribution type belongs to another coop.")

    asset = _settlement_account(cooperative, channel)
    occurred_at = occurred_at or timezone.now()

    journal = post_journal(
        cooperative=cooperative,
        reference=_reference(),
        occurred_at=occurred_at,
        memo=f"{contribution_type.name} — {membership.member_no}",
        created_by=recorded_by,
        lines=[
            Line(account=asset, debit=amount,
                 description=f"{channel} settlement"),
            Line(account=contribution_type.gl_account, credit=amount,
                 membership=membership, description=contribution_type.name),
        ],
    )

    return Contribution.objects.create(
        cooperative=cooperative,
        membership=membership,
        contribution_type=contribution_type,
        amount=amount,
        channel=channel,
        status=Contribution.Status.CONFIRMED,
        psp_reference=psp_reference,
        recorded_by=recorded_by,
        occurred_at=occurred_at,
        journal=journal,
    )


@transaction.atomic
def initiate_contribution(
    *,
    cooperative,
    membership,
    contribution_type,
    amount,
    channel: str = "psp",
    psp_reference: str,
    occurred_at=None,
    recorded_by=None,
):
    """Create a PENDING contribution awaiting PSP confirmation.

    No ledger journal is posted yet — the money hasn't settled. The PSP webhook
    (see :mod:`payments`) confirms it, which posts the balanced journal.
    """
    from contributions.models import Contribution

    amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if amount <= 0:
        raise ContributionError("Contribution amount must be positive.")
    if not psp_reference:
        raise ContributionError("A PSP reference is required to initiate.")
    if membership.cooperative_id != cooperative.id:
        raise ContributionError("Member does not belong to this cooperative.")
    if contribution_type.cooperative_id != cooperative.id:
        raise ContributionError("Contribution type belongs to another coop.")

    # Idempotent on the PSP reference: an offline-queued payment (or a retried
    # request) replayed with the same reference returns the existing record
    # rather than creating a duplicate PENDING contribution.
    existing = Contribution.all_objects.filter(
        cooperative=cooperative, psp_reference=psp_reference,
    ).first()
    if existing is not None:
        return existing

    return Contribution.objects.create(
        cooperative=cooperative,
        membership=membership,
        contribution_type=contribution_type,
        amount=amount,
        channel=channel,
        status=Contribution.Status.PENDING,
        psp_reference=psp_reference,
        recorded_by=recorded_by,
        occurred_at=occurred_at or timezone.now(),
        journal=None,
    )


@transaction.atomic
def confirm_contribution(contribution, *, recorded_by=None):
    """Post the ledger journal for a PENDING contribution → CONFIRMED.

    Idempotent: confirming an already-confirmed contribution is a no-op, so a
    duplicate webhook can never double-post to the ledger.
    """
    from contributions.models import Contribution

    if contribution.status == Contribution.Status.CONFIRMED:
        return contribution
    if contribution.status == Contribution.Status.REVERSED:
        raise ContributionError("Cannot confirm a reversed contribution.")

    coop = contribution.cooperative
    asset = _settlement_account(coop, contribution.channel)
    journal = post_journal(
        cooperative=coop,
        reference=_reference(),
        occurred_at=contribution.occurred_at,
        memo=f"{contribution.contribution_type.name} — "
             f"{contribution.membership.member_no}",
        created_by=recorded_by,
        lines=[
            Line(account=asset, debit=contribution.amount,
                 description=f"{contribution.channel} settlement"),
            Line(account=contribution.contribution_type.gl_account,
                 credit=contribution.amount, membership=contribution.membership,
                 description=contribution.contribution_type.name),
        ],
    )
    contribution.status = Contribution.Status.CONFIRMED
    contribution.journal = journal
    contribution.save(update_fields=["status", "journal", "updated_at"])
    return contribution


@transaction.atomic
def reverse_contribution(contribution, *, recorded_by=None, memo: str = ""):
    """Reverse a contribution (e.g. a duplicate) via a reversing journal."""
    from contributions.models import Contribution

    if contribution.status == Contribution.Status.REVERSED:
        raise ContributionError("Contribution is already reversed.")
    if contribution.journal is None:
        raise ContributionError("Contribution has no journal to reverse.")

    reversal = reverse_journal(contribution.journal, created_by=recorded_by,
                               memo=memo)
    contribution.status = Contribution.Status.REVERSED
    contribution.save(update_fields=["status", "updated_at"])

    from audit.services import record_action
    record_action(
        cooperative=contribution.cooperative, actor=recorded_by,
        action=f"Posted reversing entry {reversal.reference}",
        entity=contribution.journal,
        after={"reversal_of": contribution.journal.reference,
               "reason": memo or "correction"},
    )
    return contribution
