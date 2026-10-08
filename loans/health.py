"""
Loan state that should not be possible, and loans simply waiting to be paid.

Two different things live here, and keeping them apart is the whole point.

**A backlog is not a defect.** Before approval began paying loans out, approving
one and disbursing it were separate steps, so every loan approved back then is
legitimately APPROVED and undisbursed. Those rows are not damaged — the work was
just never done. They are reported as ``awaiting_payment`` at ``info`` severity,
with the reason each one cannot be paid automatically, so they read as a queue
rather than as corruption.

**An inconsistency is a defect.** A loan marked DISBURSED with no journal behind
it claims money moved while the books disagree; a payout that failed while the
loan still says disbursed means the compensation never ran. Those need
correcting.

Some corrections are safe to automate and some are not, and the line is drawn in
one place: anything that would **post or reverse a ledger entry is never done in
bulk**. Corrections in this project are made by reversal with an audit trail, and
a batch job quietly writing journals is how a ledger stops being answerable. So
rebuilding a schedule, rechecking a transfer or fixing a status is automatic;
deciding what to do about a disbursement with no journal is a person's job.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

ZERO = Decimal("0.00")

# ── Finding codes ───────────────────────────────────────────────────────────
AWAITING_PAYMENT = "awaiting_payment"
DISBURSED_WITHOUT_JOURNAL = "disbursed_without_journal"
PAYOUT_FAILED_STILL_DISBURSED = "payout_failed_still_disbursed"
PAYOUT_UNCONFIRMED = "payout_unconfirmed"
MISSING_SCHEDULE = "missing_schedule"
SETTLED_NOT_MARKED_REPAID = "settled_not_marked_repaid"
REPAID_WITH_BALANCE = "repaid_with_balance"
APPROVED_WITH_SUCCESSFUL_PAYOUT = "approved_with_successful_payout"


@dataclass
class Finding:
    code: str
    severity: str          # critical | warning | info
    loan_id: int
    cooperative_id: int
    member: str
    amount: str
    title: str
    detail: str
    fix: str               # what correcting it means, in words
    auto_fixable: bool

    def as_dict(self) -> dict:
        return asdict(self)


# A payout the provider has not confirmed yet is normal for a while — the
# webhook takes seconds to minutes. Only once it has been in flight longer than
# this is it worth an officer's attention, otherwise every fresh disbursement
# would appear in the report and the report would stop being read.
UNCONFIRMED_AFTER = timedelta(hours=1)


def loan_health(cooperative=None, *, unconfirmed_after=UNCONFIRMED_AFTER) -> dict:
    """Classify every loan. Read-only — this never writes anything.

    Payouts and instalment counts are fetched in one query each. ``outstanding``
    is still one aggregate per disbursed loan, deliberately: it is the single
    place the "only confirmed repayments count" rule lives, and duplicating that
    rule here to save a query is how two parts of a system start disagreeing
    about what a member owes.

    Pass a cooperative to scope it, or omit for the whole platform (the
    management command does the latter). ``unconfirmed_after`` is the grace
    period before an unsettled payout counts as worth looking at.
    """
    from django.db.models import Count
    from django.utils import timezone

    from loans.models import Loan, RepaymentInstalment
    from payments.models import Payout
    from payments.services import wallet_balance

    loans = Loan.all_objects.select_related("membership__user", "cooperative")
    if cooperative is not None:
        loans = loans.filter(cooperative=cooperative)
    loans = list(loans)

    loan_ids = [loan.pk for loan in loans]

    # Newest payout first per loan, so a retry wins over the attempt before it.
    payouts: dict[int, list] = {}
    if loans:
        for payout in Payout.all_objects.filter(
            kind=Payout.Kind.LOAN, object_id__in=loan_ids,
        ):
            payouts.setdefault(payout.object_id, []).append(payout)

    # Counted through ``all_objects``, in one query, and deliberately not via
    # ``loan.instalments``. RepaymentInstalment is tenant-scoped, so the reverse
    # manager fails closed whenever no tenant is bound — and this function takes
    # a cooperative as an *argument* rather than binding one, which is how the
    # management command calls it. Reading through the reverse manager reported
    # every correctly disbursed loan as having no schedule at all.
    instalment_counts = dict(
        RepaymentInstalment.all_objects
        .filter(loan_id__in=loan_ids)
        .values_list("loan_id")
        .annotate(n=Count("id"))
    ) if loans else {}

    wallets: dict[int, Decimal] = {}

    def wallet_for(coop) -> Decimal:
        if coop.id not in wallets:
            wallets[coop.id] = wallet_balance(coop)
        return wallets[coop.id]

    findings: list[Finding] = []

    def add(loan, code, severity, title, detail, fix, auto_fixable):
        findings.append(Finding(
            code=code, severity=severity, loan_id=loan.pk,
            cooperative_id=loan.cooperative_id,
            member=loan.membership.user.full_name,
            amount=f"{loan.principal:,.2f}",
            title=title, detail=detail, fix=fix, auto_fixable=auto_fixable,
        ))

    for loan in loans:
        latest = (payouts.get(loan.pk) or [None])[0]
        has_schedule = instalment_counts.get(loan.pk, 0) > 0

        if loan.status == Loan.Status.APPROVED:
            if latest is not None and latest.status == Payout.Status.SUCCESS:
                add(loan, APPROVED_WITH_SUCCESSFUL_PAYOUT, "critical",
                    "Approved, but a payout already succeeded",
                    f"Payout {latest.reference} settled, yet the loan is back "
                    f"to approved. A reversal may have run that should not "
                    f"have, or the money went out twice.",
                    "Investigate before paying again — this one cannot be "
                    "corrected mechanically.",
                    False)
            else:
                # The backlog. Not a defect: say why it cannot auto-pay.
                if not loan.destination_is_payable:
                    why = ("No bank code on the loan's destination, so it "
                           "cannot be paid electronically.")
                    fix = ("Ask the member to re-pick their bank, then pay it "
                           "— or disburse it in cash.")
                elif wallet_for(loan.cooperative) < loan.amount_to_disburse:
                    # Only principal − application fee leaves the wallet.
                    why = (f"The disbursement wallet holds "
                           f"{wallet_for(loan.cooperative):,.2f}, less than the "
                           f"{loan.amount_to_disburse:,.2f} to be paid out.")
                    fix = "Fund the wallet, then pay it."
                else:
                    why = "Ready to pay — bank details and wallet are both fine."
                    fix = "Disburse it."
                add(loan, AWAITING_PAYMENT, "info",
                    "Approved but not yet paid", why, fix, False)
            continue

        if loan.status == Loan.Status.REPAID:
            # The inverse of SETTLED_NOT_MARKED_REPAID, and the worse one: the
            # member is shown "Repaid" and cannot pay, while the books still
            # say they owe. Left behind when a repayment that never arrived is
            # reversed, or when a loan was closed by hand.
            if loan.outstanding > ZERO:
                add(loan, REPAID_WITH_BALANCE, "critical",
                    "Marked repaid but money is still owed",
                    f"{loan.outstanding:,.2f} is outstanding, yet the loan is "
                    f"closed, so the member cannot make a repayment.",
                    "Reopen it as disbursed. No money moves: the balance comes "
                    "only from confirmed repayments.",
                    True)
            continue

        if loan.status != Loan.Status.DISBURSED:
            continue

        if loan.disbursement_journal_id is None:
            add(loan, DISBURSED_WITHOUT_JOURNAL, "critical",
                "Marked disbursed with no journal behind it",
                "The loan says the principal was paid out, but no ledger entry "
                "records it. Either the journal is missing or the status is "
                "wrong.",
                "A person must decide: post the disbursement that was actually "
                "made, or revert the loan to approved. Never corrected in bulk.",
                False)

        if latest is not None:
            if latest.status in (Payout.Status.FAILED, Payout.Status.REVERSED):
                add(loan, PAYOUT_FAILED_STILL_DISBURSED, "critical",
                    "Payment failed but the loan still says disbursed",
                    f"Payout {latest.reference} is {latest.status}"
                    f"{(': ' + latest.failure_reason) if latest.failure_reason else ''}. "
                    f"The member received nothing, yet the loan is accruing a "
                    f"repayment schedule.",
                    "Reverse the disbursement and return the loan to approved — "
                    "the same compensation the provider webhook performs.",
                    True)
            elif latest.needs_recheck:
                # In flight is fine; in flight for a long time is not.
                sent = latest.sent_at or latest.created_at
                if timezone.now() - sent > unconfirmed_after:
                    waited = timezone.now() - sent
                    hours = int(waited.total_seconds() // 3600)
                    add(loan, PAYOUT_UNCONFIRMED, "warning",
                        "Payment never confirmed by the provider",
                        f"Payout {latest.reference} has been "
                        f"{latest.get_status_display().lower()} for "
                        f"{hours}h. A webhook may have been missed, so it is "
                        f"unknown whether the money arrived.",
                        "Ask the provider (recheck) and apply whatever it "
                        "reports.",
                        True)

        if not has_schedule:
            add(loan, MISSING_SCHEDULE, "warning",
                "Disbursed with no repayment schedule",
                "There are no instalments, so nothing will ever fall due and "
                "the loan cannot go into arrears.",
                "Rebuild the schedule from the disbursement date.",
                True)

        if loan.outstanding <= ZERO:
            add(loan, SETTLED_NOT_MARKED_REPAID, "warning",
                "Fully repaid but still marked disbursed",
                "Nothing is outstanding, yet the loan is not closed, so it "
                "still counts towards active lending.",
                "Mark it repaid.",
                True)

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1

    defects = [f for f in findings if f.code != AWAITING_PAYMENT]
    return {
        "checked": len(loans),
        "findings": findings,
        "counts": counts,
        "awaiting_payment": counts.get(AWAITING_PAYMENT, 0),
        "defects": len(defects),
        "critical": len([f for f in defects if f.severity == "critical"]),
        "auto_fixable": len([f for f in defects if f.auto_fixable]),
    }


def fix_finding(finding: Finding, *, actor=None) -> str:
    """Apply the safe correction for one finding. Returns what was done.

    Refuses anything not marked ``auto_fixable``: those either touch the ledger
    or mean something unexplained happened, and both need a person.
    """
    from django.db import transaction

    from audit.services import record_action
    from loans.models import Loan
    from loans.services import build_schedule, revert_disbursement
    from payments.models import Payout
    from payments.services import recheck_payout

    if not finding.auto_fixable:
        raise ValueError(
            f"{finding.code} is not safe to correct automatically: "
            f"{finding.fix}")

    loan = Loan.all_objects.get(pk=finding.loan_id)

    with transaction.atomic():
        if finding.code == PAYOUT_FAILED_STILL_DISBURSED:
            payout = (Payout.all_objects
                      .filter(kind=Payout.Kind.LOAN, object_id=loan.pk)
                      .first())
            revert_disbursement(
                loan, reason=(payout.failure_reason if payout else ""),
                actor=actor)
            done = "reverted to approved; the payment had failed"

        elif finding.code == PAYOUT_UNCONFIRMED:
            payout = (Payout.all_objects
                      .filter(kind=Payout.Kind.LOAN, object_id=loan.pk)
                      .first())
            if payout is None:
                return "no payout found to recheck"
            before = payout.status
            payout = recheck_payout(payout, actor=actor)
            done = (f"rechecked: {before} → {payout.status}"
                    if payout.status != before
                    else f"rechecked: provider still reports {payout.status}")

        elif finding.code == MISSING_SCHEDULE:
            rows = build_schedule(loan)
            done = f"rebuilt the schedule ({len(rows)} instalments)"

        elif finding.code == REPAID_WITH_BALANCE:
            from loans.services import reopen_repaid_loan

            if not reopen_repaid_loan(loan):
                return "nothing to do: the loan is no longer owing"
            done = "reopened as disbursed; the schedule matches the repayments"

        elif finding.code == SETTLED_NOT_MARKED_REPAID:
            loan.status = Loan.Status.REPAID
            loan.save(update_fields=["status", "updated_at"])
            done = "marked repaid"

        else:
            raise ValueError(f"No fixer for {finding.code}")

        record_action(
            cooperative=loan.cooperative, actor=actor,
            actor_label="" if actor else "System",
            action=f"loan.health_fix.{finding.code}", entity=loan,
            after={"loan": loan.pk, "finding": finding.code, "result": done},
        )
    return done
