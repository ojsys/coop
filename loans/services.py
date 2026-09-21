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

    body = (
        f"Your {loan.product.name} of {_money(loan.principal)} has been "
        f"disbursed. Total repayable is {_money(loan.total_repayable)} over "
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
    notify_member(loan.membership, kind=Notification.Kind.LOAN,
                  title="Repayment received", body=body)


def _notify_officers(coop, *, title, body):
    """In-app notify every privileged (officer) member of the cooperative."""
    from accounts.models import Membership
    from communications.models import Notification
    from communications.services import notify

    officers = [
        m for m in Membership.all_objects.filter(
            cooperative=coop, status=Membership.Status.ACTIVE,
        ).select_related("role")
        if m.role and m.role.is_privileged
    ]
    for member in officers:
        notify(member, kind=Notification.Kind.LOAN, title=title, body=body)


def _money(value) -> str:
    # Plain-ASCII Naira so it renders in emails everywhere.
    return f"N {Decimal(str(value)):,.2f}"


# --------------------------------------------------------------------------- #
# Approve / disburse
# --------------------------------------------------------------------------- #
def approve_loan(loan, *, actor=None, approve=True):
    from loans.models import Loan
    if loan.status != Loan.Status.PENDING:
        raise LoanError("Only a pending loan can be approved or rejected.")
    loan.status = Loan.Status.APPROVED if approve else Loan.Status.REJECTED
    loan.decided_by = actor
    loan.decided_at = timezone.now()
    loan.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    _notify_decision(loan, approve)
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
def disburse_loan(loan, *, actor=None, from_account_code="1000"):
    """Pay out an approved loan: book the principal as a receivable and out of
    cash, build the reducing-balance schedule, and notify the member.

    Interest is *not* recognised here — on a reducing-balance loan it is earned
    over the life of the loan, so it is booked to income on each repayment."""
    from ledger.models import Account
    from ledger.services import Line, post_journal
    from loans.models import Loan

    if loan.status != Loan.Status.APPROVED:
        raise LoanError("Only an approved loan can be disbursed.")

    coop = loan.cooperative
    receivable = _account(coop, "1200", "Loans Receivable",
                          Account.Kind.ASSET)
    cash = Account.all_objects.get(cooperative=coop, code=from_account_code)

    journal = post_journal(
        cooperative=coop, reference=f"LOAN-{loan.id}-DISB",
        lines=[
            Line(account=receivable, debit=loan.principal,
                 membership=loan.membership,
                 description=f"Loan {loan.id} disbursed"),
            Line(account=cash, credit=loan.principal,
                 description=f"Loan {loan.id} principal out"),
        ],
        memo=f"Loan {loan.id} disbursement", created_by=actor)

    loan.status = Loan.Status.DISBURSED
    loan.disbursed_at = timezone.now()
    loan.save(update_fields=["status", "disbursed_at", "updated_at"])
    build_schedule(loan)
    _notify_disbursed(loan)
    return journal


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


def _post_repayment(loan, amount, *, actor, channel, psp_reference, status,
                    note=""):
    """Post the ledger entries for a settled repayment and record it.

    Cash comes in; the interest portion is booked to income and the principal
    portion reduces the receivable (reducing-balance recognition)."""
    from ledger.models import Account
    from ledger.services import Line, post_journal
    from loans.models import LoanRepayment

    coop = loan.cooperative
    receivable = Account.all_objects.get(cooperative=coop, code="1200")
    cash = Account.all_objects.get(cooperative=coop, code="1000")
    interest_income = _account(coop, "4100", "Loan Interest Income",
                               Account.Kind.INCOME)

    interest_part = _allocate(loan, amount)
    principal_part = Decimal(str(amount)) - interest_part

    lines = [Line(account=cash, debit=amount,
                  description=f"Loan {loan.id} repayment")]
    if principal_part > ZERO:
        lines.append(Line(account=receivable, credit=principal_part,
                          membership=loan.membership,
                          description=f"Loan {loan.id} principal"))
    if interest_part > ZERO:
        lines.append(Line(account=interest_income, credit=interest_part,
                          description=f"Loan {loan.id} interest"))

    seq = LoanRepayment.all_objects.filter(loan=loan).count() + 1
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
def confirm_loan_repayment(repayment, *, actor=None):
    """Settle a PENDING repayment once its payment is verified: posts the ledger,
    allocates to the schedule and notifies the member. Idempotent."""
    from loans.models import LoanRepayment
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

    settled = _post_repayment(
        loan, amount, actor=actor, channel=repayment.channel,
        psp_reference=repayment.psp_reference, note=repayment.note,
        status=LoanRepayment.Status.CONFIRMED)
    # Replace the pending placeholder with the settled record.
    repayment.delete()
    return settled


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
    loan = repayment.loan
    amount = repayment.amount
    notify_member(
        loan.membership, kind=Notification.Kind.LOAN,
        title="Reported transfer not confirmed",
        body=(f"We couldn't confirm the {_money(amount)} transfer you reported "
              f"for your {loan.product.name}. Please check the details or "
              f"contact your society."))
    repayment.delete()
