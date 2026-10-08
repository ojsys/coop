"""Loan lifecycle: approve, disburse, schedule and record repayments.

Disbursement builds an even monthly repayment schedule and books the loan
through the double-entry ledger. Repayments (officer-recorded, direct transfer
or online via Paystack) are allocated to the earliest unpaid instalment first.
Every member-visible transition also fires an in-app + email notification.
"""
from __future__ import annotations

import calendar
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

ZERO = Decimal("0.00")


class LoanError(Exception):
    """A loan operation was invalid for the loan's current state."""


def _account(cooperative, code, name, kind):
    from ledger.models import Account
    account, _ = Account.all_objects.get_or_create(
        cooperative=cooperative, code=code,
        defaults={"name": name, "kind": kind, "system": True})
    return account


def _add_months(d, months):
    """Return ``d`` shifted forward by ``months`` calendar months (clamped to the
    last valid day of the target month)."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


# --------------------------------------------------------------------------- #
# Member notifications
# --------------------------------------------------------------------------- #
def _coop_bank_line(loan) -> str:
    coop = loan.cooperative
    if coop.bank_account_no:
        return (f"Bank transfer: {coop.bank_account_name or coop.name}, "
                f"{coop.bank_name} — {coop.bank_account_no}.")
    return "Bank transfer details are available from your society office."


def _notify_disbursed(loan):
    from communications.models import Notification
    from communications.services import notify_member

    fee_note = (
        f" You receive {_money(loan.amount_to_disburse)} after the "
        f"{_money(loan.application_fee)} application fee."
        if loan.application_fee > ZERO else "")
    body = (
        f"Your {loan.product.name} of {_money(loan.principal)} has been "
        f"disbursed.{fee_note} Total repayable is {_money(loan.total_repayable)} over "
        f"{loan.term_months} month(s) at {_money(loan.monthly_instalment)} a "
        f"month.\n\nHow to repay: pay online with Paystack from the app, or by "
        f"direct transfer to your society. {_coop_bank_line(loan)}"
    )
    notify_member(loan.membership, kind=Notification.Kind.LOAN,
                  title="Loan disbursed", body=body)


def _notify_decision(loan, approved):
    from communications.models import Notification
    from communications.services import notify_member

    if approved:
        title = "Loan approved"
        body = (f"Your {loan.product.name} application for "
                f"{_money(loan.principal)} was approved and is awaiting "
                f"disbursement.")
    else:
        title = "Loan not approved"
        body = (f"Your {loan.product.name} application for "
                f"{_money(loan.principal)} was not approved. Please contact "
                f"your society for details.")
    notify_member(loan.membership, kind=Notification.Kind.LOAN,
                  title=title, body=body)


def _notify_repayment(loan, amount):
    from communications.models import Notification
    from communications.services import notify_member

    if loan.status == loan.Status.REPAID:
        body = (f"We received {_money(amount)}. Your {loan.product.name} is now "
                f"fully repaid. Thank you!")
    else:
        body = (f"We received {_money(amount)} toward your {loan.product.name}. "
                f"Outstanding balance: {_money(loan.outstanding)}.")
    # In-app carries the short version; the emailed receipt carries the figures
    # the member keeps, so suppress the duplicate plain email here.
    notify_member(loan.membership, kind=Notification.Kind.LOAN,
                  title="Repayment received", body=body, email=False)

    from communications.receipts import send_loan_repayment_receipt
    send_loan_repayment_receipt(loan, amount)


def _notify_officers(coop, *, title, body, rows=None):
    """Alert every privileged (officer) member — in-app and by email.

    In-app alone was not enough: an officer who doesn't open the console has no
    idea a member is waiting on them.
    """
    from accounts.models import Membership
    from communications.models import Notification
    from communications.receipts import notify_officers_by_email
    from communications.services import notify

    officers = [
        m for m in Membership.all_objects.filter(
            cooperative=coop, status=Membership.Status.ACTIVE,
        ).select_related("role")
        if m.role and m.role.is_privileged
    ]
    for member in officers:
        notify(member, kind=Notification.Kind.LOAN, title=title, body=body)

    notify_officers_by_email(coop, subject=title, heading=title,
                             intro=body, rows=rows or [])


def _money(value) -> str:
    # Plain-ASCII Naira so it renders in emails everywhere.
    return f"N {Decimal(str(value)):,.2f}"


# --------------------------------------------------------------------------- #
# Approve / disburse
# --------------------------------------------------------------------------- #
def approve_loan(loan, *, actor=None, approve=True, auto_disburse=True):
    """Approve or reject a loan, and pay it out where that is possible.

    Approving sends the money immediately — the point of the whole payout
    rail — but only when it actually can: the loan needs a bank code and
    account number on its snapshot, and the disbursement wallet needs to cover
    the principal. When either is missing the loan stays APPROVED for the cash
    path, and the officer is told which it was rather than left wondering why
    nothing moved.

    Two transient attributes are set on the returned loan for the caller to
    report: ``auto_disbursement`` (the Payout, when one was sent) and
    ``auto_disbursement_error`` (why not). They are deliberately not model
    fields — the Payout record and the ledger are the durable history, and a
    status column that could disagree with them would be a second source of
    truth about whether money moved.
    """
    from loans.models import Loan

    if loan.status != Loan.Status.PENDING:
        raise LoanError("Only a pending loan can be approved or rejected.")
    loan.status = Loan.Status.APPROVED if approve else Loan.Status.REJECTED
    loan.decided_by = actor
    loan.decided_at = timezone.now()
    loan.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    _notify_decision(loan, approve)

    loan.auto_disbursement = None
    loan.auto_disbursement_error = None
    if not (approve and auto_disburse):
        return loan

    from core.entitlements import ELECTRONIC_PAYOUTS, has_feature

    if not has_feature(loan.cooperative, ELECTRONIC_PAYOUTS):
        # The plan pays loans by hand, so this is the normal path, not a
        # failure: no officer alert, which would otherwise fire on every
        # approval for every society on such a plan.
        loan.auto_disbursement_error = (
            "Approved. Your plan does not include electronic payouts, so "
            "disburse it in cash or by bank transfer.")
        return loan

    from payments.providers import PaymentInitError
    from payments.services import PayoutError

    try:
        loan.auto_disbursement = disburse_loan_electronically(loan, actor=actor)
    except (LoanError, PayoutError, PaymentInitError) as exc:
        # Left APPROVED on purpose: the loan is still good, it just has to be
        # paid another way. Officers are told, because an approved loan nobody
        # noticed could not be paid is how a member ends up waiting silently.
        loan.auto_disbursement_error = str(exc)
        _notify_officers(
            loan.cooperative,
            title="Loan approved but not yet paid",
            body=(f"Loan {loan.id} for {loan.membership.user.full_name} was "
                  f"approved but could not be paid out automatically:\n\n"
                  f"{exc}\n\nIt is waiting as an approved loan."),
        )
    return loan


def build_schedule(loan):
    """Create the fixed-repayment schedule for a disbursed loan.

    Idempotent — clears and rebuilds so a re-disbursement can't duplicate rows.
    Each instalment records the principal/interest split so interest can be
    booked as it is earned on each repayment.
    """
    from loans.amortization import flat_schedule
    from loans.models import RepaymentInstalment

    RepaymentInstalment.all_objects.filter(loan=loan).delete()
    start = (loan.disbursed_at or timezone.now()).date()

    rows = []
    for row in flat_schedule(loan.principal, loan.interest_rate,
                             loan.term_months):
        rows.append(RepaymentInstalment(
            cooperative=loan.cooperative, loan=loan, sequence=row.sequence,
            due_date=_add_months(start, row.sequence), amount_due=row.payment,
            principal_component=row.principal, interest_component=row.interest))
    RepaymentInstalment.all_objects.bulk_create(rows)
    return rows


@transaction.atomic
def _fee_lines(loan):
    """The application fee's ledger lines, booked with the disbursement.

        debit  1200 Loans Receivable    fee   (the member owes it)
        credit 4110 Loan Fee Income     fee   (earned when the loan is paid out)

    Together with the cash/wallet leg for ``amount_to_disburse`` this makes the
    receivable the full principal while only principal − fee leaves the
    society. Empty when the loan carries no fee.
    """
    from ledger.models import Account
    from ledger.services import Line

    if loan.application_fee <= ZERO:
        return []
    coop = loan.cooperative
    receivable = _account(coop, "1200", "Loans Receivable",
                          Account.Kind.ASSET)
    fee_income = _account(coop, "4110", "Loan Fee Income",
                          Account.Kind.INCOME)
    return [
        Line(account=receivable, debit=loan.application_fee,
             membership=loan.membership,
             description=f"Loan {loan.id} application fee"),
        Line(account=fee_income, credit=loan.application_fee,
             description=f"Loan {loan.id} application fee"),
    ]


def _check_fee(loan):
    if loan.application_fee >= loan.principal:
        raise LoanError(
            f"The application fee ({_money(loan.application_fee)}) is not less "
            f"than the loan ({_money(loan.principal)}), so there is nothing "
            f"to pay out.")


def disburse_loan(loan, *, actor=None, from_account_code="1000"):
    """Pay out an approved loan: book the principal as a receivable and out of
    cash, build the reducing-balance schedule, and notify the member.

    The application fee is deducted here: only ``amount_to_disburse`` leaves
    cash, the fee is booked to income, and the receivable is the full
    principal (see ``_fee_lines``). All in one journal, so reversing the
    disbursement reverses the fee with it.

    Interest is *not* recognised here — on a reducing-balance loan it is earned
    over the life of the loan, so it is booked to income on each repayment."""
    from ledger.models import Account
    from ledger.services import Line, post_journal
    from loans.models import Loan

    if loan.status != Loan.Status.APPROVED:
        raise LoanError("Only an approved loan can be disbursed.")
    _check_fee(loan)

    coop = loan.cooperative
    receivable = _account(coop, "1200", "Loans Receivable",
                          Account.Kind.ASSET)
    cash = Account.all_objects.get(cooperative=coop, code=from_account_code)
    paid_out = loan.amount_to_disburse

    journal = post_journal(
        cooperative=coop, reference=f"LOAN-{loan.id}-DISB",
        lines=[
            Line(account=receivable, debit=paid_out,
                 membership=loan.membership,
                 description=f"Loan {loan.id} disbursed"),
            Line(account=cash, credit=paid_out,
                 description=f"Loan {loan.id} principal out"),
            *_fee_lines(loan),
        ],
        memo=f"Loan {loan.id} disbursement", created_by=actor)

    _finalise_disbursement(loan, journal)
    return journal


def _finalise_disbursement(loan, journal=None):
    """Mark a loan disbursed, build its schedule and tell the member.

    Shared by both disbursement paths so they cannot drift: cash posts its own
    journal crediting Cash, electronic posts one crediting the wallet, and
    everything *after* the money moves is identical.

    ``journal`` is stored on the loan so that "disbursed" can later be checked
    against a real ledger entry rather than inferred from a reference pattern.
    """
    from loans.models import Loan

    loan.status = Loan.Status.DISBURSED
    loan.disbursed_at = timezone.now()
    loan.disbursement_journal = journal
    loan.save(update_fields=["status", "disbursed_at", "disbursement_journal",
                             "updated_at"])
    build_schedule(loan)
    _notify_disbursed(loan)
    return loan


@transaction.atomic
def refresh_pending_destinations(membership) -> int:
    """Re-snapshot every PENDING loan after this member's bank details changed.

    While a loan is still pending, its destination is *meant* to track the
    member: somebody who applied before supplying an account would otherwise be
    left with a permanently blank destination and a loan that can never be paid.
    From APPROVED onward the snapshot freezes, which is the control that stops a
    payout being redirected away from the account an officer vetted.

    Lives here rather than in a serializer because three different paths change a
    member's bank details — the member's own profile, an officer through
    /members/<id>/, and staff in the Django admin — and only the first one used to
    do this. The result was behaviour that depended on *who* typed the details:
    the same edit refreshed a pending loan or did not, with nothing on screen to
    say which.

    Returns how many loans were updated.
    """
    from loans.models import Loan

    pending = Loan.all_objects.filter(membership=membership,
                                      status=Loan.Status.PENDING)
    updated = 0
    for loan in pending:
        loan.snapshot_destination()
        loan.save(update_fields=["destination_bank_name",
                                 "destination_bank_code",
                                 "destination_account_no", "updated_at"])
        updated += 1
    return updated


def refresh_loan_destination(loan, *, actor=None):
    """Re-point an approved loan at the member's current bank details.

    The deliberate counterpart to the freeze. Approval captures where the money
    will go so that a member cannot redirect a vetted payout afterwards — but
    that leaves a loan approved before the member had a usable account stuck
    forever: the member supplies their bank code, and the loan still carries the
    blank snapshot it was approved with, unpayable with nothing to act on.

    So an officer may refresh it, as an explicit act that is audited with both
    the old and the new destination. Never automatic: a payout destination that
    changed quietly between approval and disbursement is precisely what the
    freeze exists to prevent.

    Refused once the money has gone — there is nothing left to redirect, and the
    destination on a disbursed loan is the record of where it actually went.
    """
    from django.db import transaction

    from audit.services import record_action
    from loans.models import Loan

    if loan.status not in (Loan.Status.PENDING, Loan.Status.APPROVED):
        raise LoanError(
            f"Loan {loan.pk} is {loan.get_status_display().lower()}. Its "
            f"destination records where the money actually went and cannot be "
            f"changed.")

    before = {
        "bank_name": loan.destination_bank_name,
        "bank_code": loan.destination_bank_code,
        "account_no": loan.destination_account_no,
    }
    loan.snapshot_destination()
    after = {
        "bank_name": loan.destination_bank_name,
        "bank_code": loan.destination_bank_code,
        "account_no": loan.destination_account_no,
    }

    if before == after:
        # Nothing to record: saying "updated" when nothing moved would make the
        # audit trail less trustworthy, not more complete.
        return loan

    with transaction.atomic():
        loan.save(update_fields=["destination_bank_name",
                                 "destination_bank_code",
                                 "destination_account_no", "updated_at"])
        record_action(
            cooperative=loan.cooperative, actor=actor,
            actor_label="" if actor else "System",
            action="loan.refresh_destination", entity=loan,
            before=before, after=after,
        )
    return loan


def unwind_disbursement(loan, *, reason="", actor=None):
    """Undo a disbursement completely — the ledger entry included.

    ``revert_disbursement`` deliberately undoes only the *loan* side, because its
    caller is the payment layer, which has already reversed the payout's journal
    by the time it runs. Anything else that needs to undo a disbursement — an
    operator correcting a mistake, say — has to reverse the journal itself, or
    the books keep saying money left while the loan says it never did.

    The signal for whether that reversal is still owed is
    ``loan.disbursement_journal``: the payment layer clears it when it unwinds,
    so a link that is still set means the entry is still live. That holds for
    both paths — the cash journal and the payout's — so this one function
    handles either.

    Corrections are made by posting the mirror image, never by deleting: the
    original entry stays in the ledger and the reversal sits beside it.
    """
    from django.db import transaction

    from ledger.services import reverse_journal
    from loans.models import Loan

    if loan.status != Loan.Status.DISBURSED:
        raise LoanError(
            f"Loan {loan.pk} is {loan.get_status_display().lower()}, not "
            f"disbursed — there is no disbursement to undo.")

    with transaction.atomic():
        journal = loan.disbursement_journal
        if journal is not None and not journal.is_reversed:
            reverse_journal(
                journal, created_by=actor,
                memo=(f"Disbursement of loan {loan.pk} reversed"
                      f"{(': ' + reason) if reason else '.'}"),
            )
        return revert_disbursement(loan, reason=reason, actor=actor)


def return_to_pending(loan, *, reason="", actor=None):
    """Put an approved loan back to PENDING, undoing the approval itself.

    Separate from ``unwind_disbursement`` because it moves nothing: the approval
    decision is cleared so the loan returns to the queue for a fresh one. A
    disbursed loan is refused rather than quietly unwound — money has moved, and
    reversing a ledger entry is not something to do as a side effect of a status
    change.
    """
    from django.db import transaction

    from audit.services import record_action
    from loans.models import Loan

    if loan.status == Loan.Status.DISBURSED:
        raise LoanError(
            f"Loan {loan.pk} has been disbursed. Reverse the disbursement "
            f"first — that posts a ledger reversal — then return it to pending.")
    if loan.status == Loan.Status.PENDING:
        return loan

    before = loan.status
    with transaction.atomic():
        loan.status = Loan.Status.PENDING
        # The decision is being withdrawn, so the record of who made it goes
        # too; leaving it would show the loan as decided by someone who no
        # longer has, and a later approval would overwrite it anyway.
        loan.decided_by = None
        loan.decided_at = None
        loan.save(update_fields=["status", "decided_by", "decided_at",
                                 "updated_at"])
        record_action(
            cooperative=loan.cooperative, actor=actor,
            actor_label="" if actor else "System",
            action="loan.return_to_pending", entity=loan,
            before={"status": before},
            after={"status": loan.status, "reason": reason},
        )
    return loan


def revert_disbursement(loan, *, reason="", actor=None):
    """Put a loan back to APPROVED after its payment failed.

    Called when the provider tells us a transfer failed or was reversed. The
    money is returned to the wallet by reversing the payout's journal; this
    undoes the *loan* side so the two stay consistent.

    The repayment schedule is deleted rather than kept: its dates are derived
    from ``disbursed_at``, so a schedule for a disbursement that never happened
    would start generating arrears for money the member never received.
    """
    from loans.models import Loan, RepaymentInstalment

    if loan.status != Loan.Status.DISBURSED:
        return loan

    loan.status = Loan.Status.APPROVED
    loan.disbursed_at = None
    # No disbursement to point at any more — the journal that moved the money
    # has been reversed, so leaving the link would make the loan look paid.
    loan.disbursement_journal = None
    loan.save(update_fields=["status", "disbursed_at", "disbursement_journal",
                             "updated_at"])
    RepaymentInstalment.all_objects.filter(loan=loan).delete()

    _notify_officers(
        loan.cooperative,
        title="Loan payment failed",
        body=(f"The payment for loan {loan.id} to "
              f"{loan.membership.user.full_name} did not go through"
              f"{(': ' + reason) if reason else '.'}\n\n"
              f"The money has been returned to the disbursement wallet and the "
              f"loan is approved again, waiting to be paid."),
    )
    return loan


@transaction.atomic
def disburse_loan_electronically(loan, *, actor=None):
    """Pay an approved loan to the member's bank, drawing on the wallet.

    Posts **no journal of its own**: send_payout writes the disbursement entry
    (debit 1200 Loans Receivable, credit 1020 Disbursement Wallet), and that
    *is* the disbursement. A second journal here would book the principal twice.

    The destination comes from the loan's own snapshot, not from the member's
    current record, so a bank-detail edit after approval cannot redirect it.

    The loan is marked disbursed once the provider accepts the transfer, which
    is also when the wallet is debited. A transfer can still fail afterwards;
    that reverses the journal and returns the loan to approved.
    """
    from core.entitlements import (ELECTRONIC_PAYOUTS, has_feature,
                                   not_included_message)
    from ledger.models import Account
    from loans.models import Loan
    from payments.models import Payout
    from payments.services import send_payout

    if loan.status != Loan.Status.APPROVED:
        raise LoanError("Only an approved loan can be disbursed.")
    if not has_feature(loan.cooperative, ELECTRONIC_PAYOUTS):
        raise LoanError(
            not_included_message(loan.cooperative, ELECTRONIC_PAYOUTS)
            + " Disburse it in cash or by bank transfer instead.")
    if not loan.destination_is_payable:
        raise LoanError(
            "This loan has no bank code and account number recorded, so it "
            "cannot be paid electronically. Disburse it in cash instead, or ask "
            "the member to re-pick their bank from the list."
        )

    _check_fee(loan)

    receivable = _account(loan.cooperative, "1200", "Loans Receivable",
                          Account.Kind.ASSET)
    # Only principal − fee is transferred; the fee rides in the payout's own
    # journal so a failed transfer reverses it along with the money.
    payout = send_payout(
        cooperative=loan.cooperative,
        amount=loan.amount_to_disburse,
        extra_lines=_fee_lines(loan),
        debit_account=receivable,
        kind=Payout.Kind.LOAN,
        object_id=loan.id,
        membership=loan.membership,
        destination_bank_name=loan.destination_bank_name,
        destination_bank_code=loan.destination_bank_code,
        destination_account_no=loan.destination_account_no,
        account_name=loan.membership.user.full_name,
        reason=f"Loan {loan.id} disbursement",
        actor=actor,
    )
    _finalise_disbursement(loan, payout.journal)
    return payout


# --------------------------------------------------------------------------- #
# Repayments
# --------------------------------------------------------------------------- #
def _allocate(loan, amount) -> Decimal:
    """Apply ``amount`` to the earliest unpaid instalments, in order.

    Returns the interest portion of the payment: each instalment's payment is
    split into interest and principal in proportion to its scheduled split, so
    the ledger can book interest income as it is earned. Principal is then the
    remainder (``amount - interest``), keeping the journal exactly balanced.
    """
    from loans.models import RepaymentInstalment

    remaining = Decimal(str(amount))
    interest_total = ZERO
    instalments = (RepaymentInstalment.all_objects
                   .filter(loan=loan)
                   .exclude(status=RepaymentInstalment.Status.PAID)
                   .order_by("sequence"))
    for inst in instalments:
        if remaining <= ZERO:
            break
        pay = min(inst.outstanding, remaining)
        if inst.amount_due > ZERO:
            interest_total += (inst.interest_component * pay
                               / inst.amount_due).quantize(Decimal("0.01"))
        inst.amount_paid += pay
        remaining -= pay
        if inst.outstanding <= ZERO:
            inst.status = RepaymentInstalment.Status.PAID
        inst.save(update_fields=["amount_paid", "status", "updated_at"])
    return interest_total


def receipt_lines(loan, amount, *, channel, receipt=None):
    """Where a repayment's money actually arrived, as ledger debit lines.

    Booked to the place the money is, because that is what the society can
    then use:

    * **cash** — the till (1000).
    * **transfer** — the member paid the society's own account: Bank (1010).
    * **psp** (Paystack) — the platform's Paystack balance, which is exactly
      what the disbursement wallet (1020) represents, so a repayment is lendable
      again at once. When the charge was split to the society's own subaccount
      the money went to its bank instead (1010).

    Paystack's fee is the member's to pay: checkout charges the repayment
    *plus* the fee (payments.fees.gross_up), so what lands is the whole
    repayment. Booked as what actually happened — the wallet gets what
    Paystack paid out, Paystack's fee is a Payment Charge (5000), and the fee
    the member paid offsets it — so Payment Charges nets to nothing, or to the
    small difference when Paystack priced the card differently (an
    international card, say). A charge made before fees were passed on carries
    no surcharge, and its fee stays the society's cost.

    Previously every repayment debited Cash, so an online repayment never
    reached the wallet the next loan is paid from.

    ``receipt`` is the provider's view of the charge:
    ``{"amount" (paid), "fee", "subaccount"}``.
    """
    from ledger.models import Account
    from ledger.services import Line

    coop = loan.cooperative
    amount = Decimal(str(amount))
    label = f"Loan {loan.id} repayment"
    if channel == "cash":
        return [Line(account=_account(coop, "1000", "Cash", Account.Kind.ASSET),
                     debit=amount, description=label)]
    bank = _account(coop, "1010", "Bank / PSP Settlement", Account.Kind.ASSET)
    if channel != "psp":
        return [Line(account=bank, debit=amount, description=label)]

    receipt = receipt or {}
    cent = Decimal("0.01")
    # What the member paid in all. Unknown (dev key) means no surcharge.
    paid = Decimal(str(receipt.get("amount") or amount)).quantize(cent)
    if paid < amount:
        paid = amount
    fee = Decimal(str(receipt.get("fee") or 0)).quantize(cent)
    if fee < ZERO or fee >= paid:
        fee = ZERO
    landed = (bank if receipt.get("subaccount") else
              _account(coop, "1020", "Disbursement Wallet",
                       Account.Kind.ASSET))
    lines = [Line(account=landed, debit=paid - fee, description=label)]
    # Paystack's fee less what the member paid towards it.
    charges = fee - (paid - amount)
    if charges:
        account = _account(coop, "5000", "Payment Charges",
                           Account.Kind.EXPENSE)
        lines.append(
            Line(account=account, debit=charges,
                 description=f"Paystack fee on loan {loan.id} repayment")
            if charges > ZERO else
            Line(account=account, credit=-charges,
                 description=f"Paystack fee paid by member, loan {loan.id}"))
    return lines


def _post_repayment(loan, amount, *, actor, channel, psp_reference, status,
                    note="", receipt=None):
    """Post the ledger entries for a settled repayment and record it.

    The money is debited where it arrived (see ``receipt_lines``); the interest
    portion is booked to income and the principal portion reduces the
    receivable (reducing-balance recognition). The member is credited the full
    amount whatever fee the provider took."""
    from ledger.models import Account
    from ledger.services import Line, post_journal
    from loans.models import LoanRepayment

    coop = loan.cooperative
    receivable = Account.all_objects.get(cooperative=coop, code="1200")
    interest_income = _account(coop, "4100", "Loan Interest Income",
                               Account.Kind.INCOME)

    interest_part = _allocate(loan, amount)
    principal_part = Decimal(str(amount)) - interest_part

    lines = receipt_lines(loan, amount, channel=channel, receipt=receipt)
    if principal_part > ZERO:
        lines.append(Line(account=receivable, credit=principal_part,
                          membership=loan.membership,
                          description=f"Loan {loan.id} principal"))
    if interest_part > ZERO:
        lines.append(Line(account=interest_income, credit=interest_part,
                          description=f"Loan {loan.id} interest"))

    # Numbered from the journals already posted, not by counting repayment
    # rows: confirming an online payment deletes its pending placeholder, so a
    # row count can fall back onto a number already used — and the next
    # repayment on that loan failed on the ledger's unique reference.
    from ledger.models import Journal

    seq = LoanRepayment.all_objects.filter(loan=loan).count() + 1
    while Journal.all_objects.filter(
            cooperative=coop, reference=f"LOAN-{loan.id}-RPY-{seq}").exists():
        seq += 1
    journal = post_journal(
        cooperative=coop, reference=f"LOAN-{loan.id}-RPY-{seq}", lines=lines,
        memo=f"Loan {loan.id} repayment", created_by=actor)

    repayment = LoanRepayment.all_objects.create(
        cooperative=coop, loan=loan, amount=amount, channel=channel,
        status=status, psp_reference=psp_reference, note=note, journal=journal)
    loan.refresh_from_db()
    if loan.outstanding <= ZERO:
        loan.status = loan.Status.REPAID
        loan.save(update_fields=["status", "updated_at"])
    _notify_repayment(loan, amount)
    return repayment


def _validate_repayment(loan, amount):
    from loans.models import Loan
    if loan.status != Loan.Status.DISBURSED:
        raise LoanError("Repayments can only be recorded on a disbursed loan.")
    amount = Decimal(str(amount))
    if amount <= 0:
        raise LoanError("Repayment amount must be positive.")
    if amount > loan.outstanding:
        raise LoanError(
            f"Amount exceeds the outstanding balance of {_money(loan.outstanding)}.")
    return amount


@transaction.atomic
def record_repayment(loan, *, amount, actor=None, channel="cash"):
    """Record a settled repayment (officer-entered cash or a confirmed transfer):
    cash in, receivable down, allocated to the schedule. Marks the loan repaid
    once fully settled."""
    from loans.models import LoanRepayment
    amount = _validate_repayment(loan, amount)
    return _post_repayment(loan, amount, actor=actor, channel=channel,
                           psp_reference="",
                           status=LoanRepayment.Status.CONFIRMED)


@transaction.atomic
def initiate_loan_repayment(loan, *, amount, reference, channel="psp"):
    """Start an online (Paystack) repayment: records a PENDING repayment that a
    later ``confirm`` (verify/webhook) settles. Nothing posts to the ledger yet."""
    from loans.models import LoanRepayment
    amount = _validate_repayment(loan, amount)
    return LoanRepayment.all_objects.create(
        cooperative=loan.cooperative, loan=loan, amount=amount,
        channel=channel, status=LoanRepayment.Status.PENDING,
        psp_reference=reference)


@transaction.atomic
def _provider_receipt(reference):
    """Ask Paystack for the fee and split on a charge. Best effort: with no
    answer the repayment is still booked, to the wallet with no fee."""
    from payments.models import Provider
    from payments.providers import PaymentInitError, get_provider

    try:
        return get_provider(Provider.PAYSTACK).fetch_transaction(reference)
    except PaymentInitError:
        return None


def confirm_loan_repayment(repayment, *, actor=None, receipt=None):
    """Settle a PENDING repayment once its payment is verified: posts the ledger,
    allocates to the schedule and notifies the member. Idempotent.

    ``receipt`` is the provider's account of an online payment (fee, and
    whether it was split to a subaccount); callers that already asked pass it,
    otherwise it is fetched here."""
    from loans.models import LoanRepayment
    if repayment.status == LoanRepayment.Status.CONFIRMED:
        return repayment

    # Locked, because the member's return from checkout and the provider's
    # webhook can both arrive for the same payment, and each would otherwise
    # post the repayment once.
    locked = (LoanRepayment.all_objects.select_for_update()
              .filter(pk=repayment.pk).first())
    if locked is None:
        # The other caller got here first and replaced the placeholder with
        # the settled record.
        if not repayment.psp_reference:
            return None
        return LoanRepayment.all_objects.filter(
            cooperative_id=repayment.cooperative_id,
            psp_reference=repayment.psp_reference,
            status=LoanRepayment.Status.CONFIRMED).first()
    repayment = locked
    if repayment.status == LoanRepayment.Status.CONFIRMED:
        return repayment

    loan = repayment.loan
    amount = repayment.amount
    # Re-check against the live outstanding (another repayment may have landed).
    if amount > loan.outstanding:
        amount = loan.outstanding
    if amount <= ZERO:
        # Nothing left to settle — cancel the stale pending intent.
        repayment.delete()
        return None

    if (repayment.channel == LoanRepayment.Channel.PSP and receipt is None
            and repayment.psp_reference):
        receipt = _provider_receipt(repayment.psp_reference)

    settled = _post_repayment(
        loan, amount, actor=actor, channel=repayment.channel,
        psp_reference=repayment.psp_reference, note=repayment.note,
        status=LoanRepayment.Status.CONFIRMED, receipt=receipt)
    # Replace the pending placeholder with the settled record.
    repayment.delete()
    return settled


def _reallocate_schedule(loan):
    """Make the instalments reflect exactly the confirmed repayments.

    Allocation is always oldest-instalment-first, so clearing it and replaying
    the confirmed repayments in order reproduces the state they alone produce —
    dropping anything a reversed or never-paid repayment had marked paid.
    """
    from loans.models import LoanRepayment, RepaymentInstalment

    RepaymentInstalment.all_objects.filter(loan=loan).update(
        amount_paid=ZERO, status=RepaymentInstalment.Status.PENDING)
    for kept in (LoanRepayment.all_objects
                 .filter(loan=loan, status=LoanRepayment.Status.CONFIRMED)
                 .order_by("created_at", "id")):
        _allocate(loan, kept.amount)


@transaction.atomic
def reopen_repaid_loan(loan, *, actor=None):
    """Return a loan marked REPAID to DISBURSED if money is still owed.

    Returns ``True`` if it was reopened. Moves no money and posts nothing: the
    balance is derived from confirmed repayments, so only the label and the
    schedule were wrong. Reopening is what lets the member repay again.
    """
    from loans.models import Loan

    loan.refresh_from_db()
    if loan.status != Loan.Status.REPAID or loan.outstanding <= ZERO:
        return False
    from audit.services import record_action

    _reallocate_schedule(loan)
    loan.status = Loan.Status.DISBURSED
    loan.save(update_fields=["status", "updated_at"])
    record_action(
        cooperative=loan.cooperative, actor=actor,
        actor_label="" if actor else "System",
        action="loan.reopened", entity=loan,
        before={"status": Loan.Status.REPAID},
        after={"status": loan.status, "outstanding": str(loan.outstanding)},
    )
    return True


@transaction.atomic
def reverse_repayment(repayment, *, reason, actor=None):
    """Undo a repayment that was posted in error, everywhere it took effect.

    For a repayment confirmed although no money arrived — the case that made a
    member's loan read "Repaid" with nothing paid. Four things were changed when
    it was posted, so four are put back:

    * **Ledger** — a mirror journal is posted; the original is never touched.
    * **Balance** — the repayment becomes REVERSED, and ``repaid_amount`` counts
      only confirmed ones, so the outstanding balance returns.
    * **Schedule** — instalments are re-allocated from scratch with the
      repayments that remain. Allocation is always oldest-instalment-first, so
      replaying it gives exactly the state those repayments alone produce.
    * **Status** — a loan marked REPAID goes back to DISBURSED, which is what
      lets the member repay it again.

    The member is told, since they were told it was received.
    """
    from django.utils import timezone

    from audit.services import record_action
    from communications.models import Notification
    from communications.services import notify_member
    from ledger.services import reverse_journal
    from loans.models import Loan, LoanRepayment

    repayment = LoanRepayment.all_objects.select_for_update().get(
        pk=repayment.pk)
    if repayment.status != LoanRepayment.Status.CONFIRMED:
        raise LoanError("Only a confirmed repayment can be reversed.")

    loan = Loan.all_objects.select_for_update().get(pk=repayment.loan_id)
    if repayment.journal_id and not repayment.journal.is_reversed:
        repayment.reversal_journal = reverse_journal(
            repayment.journal, created_by=actor,
            memo=f"Loan {loan.id} repayment reversed: {reason}"[:255])
    repayment.status = LoanRepayment.Status.REVERSED
    repayment.reversal_reason = reason[:255]
    repayment.reversed_at = timezone.now()
    repayment.save(update_fields=["status", "reversal_journal",
                                  "reversal_reason", "reversed_at",
                                  "updated_at"])

    before = loan.status
    _reallocate_schedule(loan)
    reopen_repaid_loan(loan, actor=actor)

    notify_member(
        loan.membership, kind=Notification.Kind.LOAN,
        title="Repayment reversed",
        body=(f"The {_money(repayment.amount)} repayment recorded on your "
              f"{loan.product.name} has been reversed: {reason} Outstanding "
              f"balance: {_money(loan.outstanding)}. You can make the "
              f"repayment again from your loan page."))
    record_action(
        cooperative=loan.cooperative, actor=actor,
        actor_label="" if actor else "System",
        action="loan.repayment_reversed", entity=repayment,
        before={"loan_status": before},
        after={"loan_status": loan.status, "amount": str(repayment.amount),
               "reference": repayment.psp_reference, "reason": reason},
    )
    return repayment


@transaction.atomic
def report_transfer(loan, *, amount, reference="", note=""):
    """A member reports a direct bank transfer they've made ("I have paid").

    Records a PENDING repayment (channel=transfer) and notifies the society's
    officers to verify it against their account and confirm. Nothing posts to
    the ledger until an officer confirms."""
    from loans.models import LoanRepayment
    amount = _validate_repayment(loan, amount)
    repayment = LoanRepayment.all_objects.create(
        cooperative=loan.cooperative, loan=loan, amount=amount,
        channel=LoanRepayment.Channel.TRANSFER,
        status=LoanRepayment.Status.PENDING,
        psp_reference=reference, note=note)
    _notify_officers(
        loan.cooperative,
        title="Repayment reported — verify",
        body=(f"{loan.membership.user.full_name} ({loan.membership.member_no}) "
              f"reported a bank transfer of {_money(amount)} for their "
              f"{loan.product.name}. Verify it against your account and confirm "
              f"it in the console."))
    return repayment


@transaction.atomic
def reject_repayment(repayment, *, actor=None):
    """Dismiss a member-reported transfer that couldn't be verified. Notifies
    the member so they can follow up. Only pending claims can be rejected."""
    from communications.models import Notification
    from communications.services import notify_member
    from loans.models import LoanRepayment

    if repayment.status != LoanRepayment.Status.PENDING:
        raise LoanError("Only a pending repayment claim can be dismissed.")
    if repayment.channel == LoanRepayment.Channel.PSP:
        # An online checkout the member never completed: nothing was reported,
        # so there is nothing to tell them. Clearing it only tidies the queue.
        repayment.delete()
        return
    loan = repayment.loan
    amount = repayment.amount
    notify_member(
        loan.membership, kind=Notification.Kind.LOAN,
        title="Reported transfer not confirmed",
        body=(f"We couldn't confirm the {_money(amount)} transfer you reported "
              f"for your {loan.product.name}. Please check the details or "
              f"contact your society."))
    repayment.delete()
