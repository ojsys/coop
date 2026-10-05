"""
Answering "was this loan actually disbursed?" from the evidence.

A status field says what someone recorded. This says what the books and the bank
can actually show, which is a different question and the one an officer needs
when a member says the money never arrived.

Two paths have to be covered and they leave different traces:

* **Cash.** A journal referenced ``LOAN-<id>-DISB`` and nothing else. There is no
  provider to ask, so the ledger entry *is* the evidence — the Payouts page
  cannot see these loans at all, which is why checking there and finding nothing
  proves nothing.
* **Transfer.** A ``Payout`` carrying the provider's reference, plus the journal
  ``send_payout`` posted. Here the provider can be asked directly, which is what
  a recheck does.

The verdict deliberately separates "nobody has confirmed this" from "this is
wrong". A transfer sent four minutes ago is simply unconfirmed; a loan marked
disbursed with no ledger entry behind it is a contradiction. Reporting both as
"problem" would train officers to ignore the screen.
"""
from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0.00")

# Verdict codes. The frontend keys its wording and tone off these.
CONFIRMED_CASH = "confirmed_cash"
CONFIRMED_TRANSFER = "confirmed_transfer"
IN_FLIGHT = "in_flight"
FAILED = "failed"
REVERSED = "reversed"
NO_JOURNAL = "no_journal"
NOT_DISBURSED = "not_disbursed"


def _journal_amount(journal) -> Decimal:
    """What the journal actually moved — the sum of its debits.

    Read from the entries rather than from the loan's principal on purpose: the
    question is what the ledger says, and if the two disagree the officer needs
    to see the ledger's number, not have it quietly replaced by the expected one.
    """
    from django.db.models import Sum

    from ledger.models import LedgerEntry

    agg = (LedgerEntry.all_objects.filter(journal=journal)
           .aggregate(total=Sum("debit")))
    # Quantised: this is rendered as money, and an aggregate comes back with
    # whatever scale the column happens to yield ("1000" rather than "1000.00").
    return (agg["total"] or ZERO).quantize(Decimal("0.01"))


def _latest_payout(loan):
    from payments.models import Payout

    return (Payout.all_objects
            .filter(kind=Payout.Kind.LOAN, object_id=loan.pk)
            .order_by("-created_at", "-id")
            .first())


def disbursement_evidence(loan) -> dict:
    """Everything known about whether this loan's principal actually left.

    Read-only. Safe to call on any loan in any status.
    """
    from loans.models import Loan

    journal = loan.disbursement_journal
    payout = _latest_payout(loan)

    evidence = {
        "loan_id": loan.pk,
        "member": loan.membership.user.full_name,
        "principal": str(loan.principal),
        "status": loan.status,
        "status_display": loan.get_status_display(),
        "disbursed_at": loan.disbursed_at,
        "journal": None,
        "payout": None,
        "method": "unknown",
        "can_recheck": False,
        # Why rechecking is unavailable, when it is. A disabled control with no
        # explanation reads as a bug.
        "recheck_unavailable_because": "",
    }

    if journal is not None:
        evidence["journal"] = {
            "id": journal.pk,
            "reference": journal.reference,
            "memo": journal.memo,
            "amount": str(_journal_amount(journal)),
            "occurred_at": journal.occurred_at,
            "posted_at": journal.posted_at,
            "is_reversed": journal.is_reversed,
        }

    if payout is not None:
        evidence["payout"] = {
            "id": payout.pk,
            "reference": payout.reference,
            "status": payout.status,
            "status_display": payout.get_status_display(),
            "summary": payout.describe(),
            "destination": (f"{payout.destination_bank_name} "
                            f"{payout.destination_account_no}").strip(),
            "failure_reason": payout.failure_reason,
            "needs_recheck": payout.needs_recheck,
            "sent_at": payout.sent_at,
            "settled_at": payout.settled_at,
        }
        evidence["method"] = "transfer"
    elif journal is not None:
        evidence["method"] = "cash"

    verdict, headline, detail = _verdict(loan, journal, payout)
    evidence["verdict"] = verdict
    evidence["headline"] = headline
    evidence["detail"] = detail

    # Only a provider can be asked, and only about a transfer that is not settled.
    if payout is None:
        evidence["recheck_unavailable_because"] = (
            "This was a cash disbursement, so there is no bank transfer to ask "
            "about. The ledger entry above is the record."
            if journal is not None else
            "No payment has been attempted for this loan yet.")
    elif payout.needs_recheck:
        evidence["can_recheck"] = True
    else:
        evidence["recheck_unavailable_because"] = (
            f"The provider has already given a final answer: "
            f"{payout.get_status_display().lower()}.")

    return evidence


def _verdict(loan, journal, payout):
    """The one-line answer, and the sentence under it."""
    from loans.models import Loan
    from payments.models import Payout

    if loan.status not in (Loan.Status.DISBURSED, Loan.Status.REPAID):
        return (NOT_DISBURSED,
                "Not disbursed.",
                f"This loan is {loan.get_status_display().lower()}. No money has "
                f"been paid out, and nothing in the ledger says otherwise.")

    if journal is None:
        return (NO_JOURNAL,
                "Cannot be confirmed — no ledger entry.",
                "The loan is marked disbursed, but there is no journal recording "
                "the principal leaving. Either the payment was never actually "
                "made, or it was made outside the system and never recorded. "
                "An officer has to establish which before this can be corrected.")

    if journal.is_reversed:
        return (REVERSED,
                "Reversed — the money came back.",
                f"The disbursement journal {journal.reference} has been reversed, "
                f"so the ledger shows the principal returned. If the loan still "
                f"reads as disbursed, that is a contradiction worth correcting.")

    if payout is None:
        return (CONFIRMED_CASH,
                "Paid in cash and recorded.",
                f"Journal {journal.reference} records the principal leaving on "
                f"{journal.occurred_at:%d %b %Y}. There was no bank transfer, so "
                f"there is no provider to confirm it with — the ledger entry is "
                f"the evidence.")

    if payout.status == Payout.Status.SUCCESS:
        return (CONFIRMED_TRANSFER,
                "Confirmed by the bank.",
                f"The provider settled transfer {payout.reference} to "
                f"{payout.destination_bank_name} "
                f"{payout.destination_account_no}, and journal "
                f"{journal.reference} records it.")

    if payout.status in (Payout.Status.FAILED, Payout.Status.REVERSED):
        reason = f": {payout.failure_reason}" if payout.failure_reason else "."
        return (FAILED,
                "The payment failed — but the loan still says disbursed.",
                f"Transfer {payout.reference} did not go through{reason} The "
                f"member received nothing, yet a repayment schedule is running "
                f"against them. Asking the provider again will not change this — "
                f"it has already given its answer. The correction is to reverse "
                f"the disbursement and put the loan back to approved, which the "
                f"loan health check lists and can apply.")

    return (IN_FLIGHT,
            "Sent, but not yet confirmed.",
            f"Transfer {payout.reference} has been handed to the provider and no "
            f"confirmation has come back. A webhook may simply be in transit, so "
            f"this is normal for a short while. Recheck to ask the bank directly.")


def recheck_disbursement(loan, *, actor=None) -> dict:
    """Ask the provider what really happened, then return fresh evidence.

    Reuses ``recheck_payout``, so the answer is reconciled through exactly the
    same code path as an inbound webhook — including the compensation that puts a
    loan back to approved when the transfer turns out to have failed. A separate
    "just tell me the status" call here would be a second source of truth about
    money, and the two would eventually disagree.
    """
    from payments.services import recheck_payout

    payout = _latest_payout(loan)
    if payout is not None and payout.needs_recheck:
        recheck_payout(payout, actor=actor)
        # The recheck may have reverted the loan, so re-read before reporting.
        loan.refresh_from_db()

    return disbursement_evidence(loan)
