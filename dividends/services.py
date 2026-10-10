"""
Declaring, approving, posting and reversing dividend distributions.

The rules this module enforces, and why:

* **Only surplus is distributed.** A dividend is paid from what the society has
  earned, never from members' savings or share capital. There is no year-end
  close in this ledger, so the distributable surplus is all income less all
  expenses to date, plus Retained Surplus (3100) — which every posted dividend
  debits, so what has been paid out is already netted off. Checked when a
  dividend is declared *and* again when it is posted, because two drafts can
  each fit on their own and not together.
* **Two officers.** Posting credits every member's withdrawable savings, so a
  draft is sent for approval and posted only when a *different* officer
  approves it (approvals.services.decide_request runs ``post_dividend``).
* **Corrected by reversal.** A posted dividend is never deleted: its journal is
  mirrored, the declaration kept as REVERSED, and the members told. Refused if a
  member has already withdrawn the money, which would leave them owing.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


class DividendError(Exception):
    """A dividend could not be declared, posted or reversed."""


def _money(value) -> str:
    return f"₦{Decimal(value):,.2f}"


# ── What may be distributed ─────────────────────────────────────────────────
def _surplus_account(cooperative):
    """The retained-surplus equity account dividends are drawn from."""
    from ledger.models import Account
    account, _ = Account.all_objects.get_or_create(
        cooperative=cooperative, code="3100",
        defaults={"name": "Retained Surplus", "kind": Account.Kind.EQUITY,
                  "system": True})
    return account


def _eligible_members(cooperative):
    from accounts.models import Membership

    # Members only: a dividend is a return on shares, and a non-member holds
    # none, whatever a stray share-capital figure on their record says.
    return list(Membership.all_objects.filter(
        cooperative=cooperative, status=Membership.Status.ACTIVE,
        kind=Membership.Kind.MEMBER,
        share_capital__gt=0).select_related("user"))


def distributable_surplus(cooperative) -> dict:
    """What the society can distribute now, and the figures behind it.

    Uses the same income statement the Reports page shows, so the number an
    officer sees here is the one their accounts already agree with.
    """
    from ledger.services import account_balance
    from reports.financials import income_statement

    statement = income_statement(cooperative)
    retained = account_balance(_surplus_account(cooperative))
    available = (statement["surplus"] + retained).quantize(CENT)
    members = _eligible_members(cooperative)
    return {
        "income_total": Decimal(statement["income_total"]).quantize(CENT),
        "expense_total": Decimal(statement["expense_total"]).quantize(CENT),
        # Negative once dividends have been paid from it.
        "retained_surplus": retained.quantize(CENT),
        "available": max(available, ZERO),
        "share_capital_base": sum((m.share_capital for m in members),
                                  ZERO).quantize(CENT),
        "eligible_members": len(members),
    }


def _check_affordable(cooperative, total):
    available = distributable_surplus(cooperative)["available"]
    if total > available:
        raise DividendError(
            f"Only {_money(available)} of surplus is available to distribute, "
            f"so {_money(total)} cannot be paid. A dividend is paid from what "
            f"the society has earned — not from members' savings or share "
            f"capital.")


# ── Declaring ───────────────────────────────────────────────────────────────
@transaction.atomic
def declare_dividend(*, cooperative, period_label, total_amount=None,
                     rate=None, note="", created_by=None):
    """Create a DRAFT declaration with its per-member allocations.

    Give either ``total_amount`` (a pool shared pro-rata to share capital) or
    ``rate`` (that % of each member's share capital). Nothing is posted to the
    ledger yet. The largest allocation absorbs any rounding remainder so the
    allocations sum exactly to the total.
    """
    from audit.services import record_action
    from dividends.models import DividendAllocation, DividendDeclaration

    period_label = (period_label or "").strip()
    if not period_label:
        raise DividendError("Give the period this dividend is for, e.g. FY2025.")
    if (total_amount in (None, "")) == (rate in (None, "")):
        raise DividendError("Give either a total amount or a rate, not both.")

    clash = DividendDeclaration.all_objects.filter(
        cooperative=cooperative, period_label__iexact=period_label,
    ).exclude(status=DividendDeclaration.Status.REVERSED).first()
    if clash is not None:
        raise DividendError(
            f"A dividend for {period_label} already exists "
            f"({clash.get_status_display().lower()}). Delete or reverse it "
            f"first, or use a different period.")

    members = _eligible_members(cooperative)
    base = sum((m.share_capital for m in members), ZERO)
    if not members or base <= 0:
        raise DividendError(
            "No active member holds share capital, so there is nobody to "
            "allocate a dividend to.")

    if rate not in (None, ""):
        rate = Decimal(str(rate))
        if rate <= 0 or rate > 100:
            raise DividendError("The rate must be between 0 and 100%.")
        method = DividendDeclaration.Method.RATE
        shares = [(m, (m.share_capital * rate / 100).quantize(
            CENT, ROUND_HALF_UP)) for m in members]
        total = sum((amount for _, amount in shares), ZERO)
    else:
        total = Decimal(str(total_amount)).quantize(CENT)
        if total <= 0:
            raise DividendError("The total must be more than zero.")
        method = DividendDeclaration.Method.POOL
        rate = None
        shares = [[m, (total * m.share_capital / base).quantize(
            CENT, ROUND_HALF_UP)] for m in members]
        remainder = total - sum((a for _, a in shares), ZERO)
        if remainder:
            max(shares, key=lambda s: s[1])[1] += remainder

    _check_affordable(cooperative, total)

    declaration = DividendDeclaration.objects.create(
        cooperative=cooperative, period_label=period_label, method=method,
        rate=rate, total_amount=total, note=note, created_by=created_by)
    DividendAllocation.objects.bulk_create([
        DividendAllocation(cooperative=cooperative, declaration=declaration,
                           membership=m, share_capital=m.share_capital,
                           amount=amount)
        for m, amount in shares if amount > 0
    ])
    record_action(
        cooperative=cooperative, actor=created_by,
        action="dividend.declared", entity=declaration,
        after={"period": period_label, "total": str(total),
               "method": method, "rate": str(rate) if rate else None,
               "members": len(shares)},
    )
    return declaration


# ── Approval ────────────────────────────────────────────────────────────────
def pending_approval(declaration):
    """The open approval request to post this declaration, if any."""
    from approvals.models import ApprovalRequest

    return ApprovalRequest.all_objects.filter(
        cooperative_id=declaration.cooperative_id,
        action=ApprovalRequest.Action.DIVIDEND_POST,
        object_id=declaration.pk,
        status=ApprovalRequest.Status.PENDING).first()


def request_posting(declaration, *, actor, note=""):
    """Send a draft for a second officer to approve. Idempotent."""
    from approvals.models import ApprovalRequest
    from approvals.services import submit_request
    from dividends.models import DividendDeclaration

    if declaration.status != DividendDeclaration.Status.DRAFT:
        raise DividendError("Only a draft dividend can be sent for approval.")
    existing = pending_approval(declaration)
    if existing is not None:
        return existing
    _check_affordable(declaration.cooperative, declaration.total_amount)
    return submit_request(
        cooperative=declaration.cooperative,
        action=ApprovalRequest.Action.DIVIDEND_POST,
        object_id=declaration.pk, requested_by=actor, note=note)


def discard_dividend(declaration):
    """Delete a draft. A posted dividend is reversed instead, never deleted."""
    from dividends.models import DividendDeclaration

    if declaration.status != DividendDeclaration.Status.DRAFT:
        raise DividendError(
            "A posted dividend cannot be deleted — its money is in members' "
            "savings. Reverse it instead.")
    request = pending_approval(declaration)
    if request is not None:
        raise DividendError(
            "This dividend is waiting for approval. Ask the approver to "
            "reject it first.")
    declaration.delete()


# ── Posting ─────────────────────────────────────────────────────────────────
@transaction.atomic
def post_dividend(declaration, *, actor=None):
    """Post the declaration: debit retained surplus, credit each member's funds.

    Reached through an approved ``dividend.post`` request. Idempotent — a posted
    declaration is left alone. Each member is told what reached their savings.
    """
    from audit.services import record_action
    from communications.models import Notification
    from communications.services import notify_member
    from dividends.models import DividendAllocation, DividendDeclaration
    from ledger.models import Account
    from ledger.services import Line, post_journal

    declaration = DividendDeclaration.all_objects.select_for_update().get(
        pk=declaration.pk)
    if declaration.status == DividendDeclaration.Status.POSTED:
        return declaration
    if declaration.status != DividendDeclaration.Status.DRAFT:
        raise DividendError("Only a draft dividend can be posted.")

    coop = declaration.cooperative
    # Use the unscoped manager: services must not depend on ambient tenant.
    allocations = list(
        DividendAllocation.all_objects.filter(declaration=declaration)
        .select_related("membership__user"))
    if not allocations:
        raise DividendError("Declaration has no allocations to post.")
    total = sum((a.amount for a in allocations), ZERO)
    _check_affordable(coop, total)

    surplus = _surplus_account(coop)
    member_funds = Account.all_objects.get(cooperative=coop, code="2000")
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

    for a in allocations:
        notify_member(
            a.membership, kind=Notification.Kind.DIVIDEND,
            title=f"Dividend for {declaration.period_label}",
            body=(f"{_money(a.amount)} has been added to your savings as your "
                  f"{declaration.period_label} dividend, on share capital of "
                  f"{_money(a.share_capital)}."))
    record_action(
        cooperative=coop, actor=actor, actor_label="" if actor else "System",
        action="dividend.posted", entity=declaration,
        after={"period": declaration.period_label, "total": str(total),
               "journal": journal.reference},
    )
    return declaration


# ── Reversal ────────────────────────────────────────────────────────────────
@transaction.atomic
def reverse_dividend(declaration, *, reason, actor=None):
    """Undo a posted dividend: mirror its journal and tell the members.

    Refused if any member's savings no longer hold their dividend (they have
    withdrawn it): reversing would leave them owing the society money it paid
    them, which is a conversation to have, not a ledger entry to post.
    """
    from audit.services import record_action
    from communications.models import Notification
    from communications.services import notify_member
    from dividends.models import DividendAllocation, DividendDeclaration
    from ledger.services import member_balance, reverse_journal

    reason = (reason or "").strip()
    if not reason:
        raise DividendError("Give a reason for reversing this dividend.")
    declaration = DividendDeclaration.all_objects.select_for_update().get(
        pk=declaration.pk)
    if declaration.status != DividendDeclaration.Status.POSTED:
        raise DividendError("Only a posted dividend can be reversed.")

    allocations = list(
        DividendAllocation.all_objects.filter(declaration=declaration)
        .select_related("membership__user"))
    short = [a for a in allocations
             if member_balance(a.membership) < a.amount]
    if short:
        names = ", ".join(a.membership.user.full_name for a in short[:5])
        more = f" and {len(short) - 5} more" if len(short) > 5 else ""
        raise DividendError(
            f"Some members no longer have their dividend in savings "
            f"({names}{more}) — it has been withdrawn. Reversing would leave "
            f"them owing the society; settle that with them first.")

    if declaration.journal_id and not declaration.journal.is_reversed:
        declaration.reversal_journal = reverse_journal(
            declaration.journal, created_by=actor,
            memo=f"Dividend {declaration.period_label} reversed: {reason}"[:255])
    declaration.status = DividendDeclaration.Status.REVERSED
    declaration.reversed_at = timezone.now()
    declaration.reversed_by = actor
    declaration.reversal_reason = reason[:255]
    declaration.save(update_fields=[
        "status", "reversal_journal", "reversed_at", "reversed_by",
        "reversal_reason", "updated_at"])

    for a in allocations:
        notify_member(
            a.membership, kind=Notification.Kind.DIVIDEND,
            title=f"Dividend for {declaration.period_label} reversed",
            body=(f"The {_money(a.amount)} dividend credited to your savings "
                  f"for {declaration.period_label} has been reversed: "
                  f"{reason}"))
    record_action(
        cooperative=declaration.cooperative, actor=actor,
        action="dividend.reversed", entity=declaration,
        after={"period": declaration.period_label,
               "total": str(declaration.total_amount), "reason": reason},
    )
    return declaration
