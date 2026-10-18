"""Submitting and deciding maker-checker approval requests."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone


class ApprovalError(Exception):
    """An approval request could not be submitted or decided."""


def officer_user_ids(cooperative) -> set[int]:
    """Everyone holding an office (any role beyond a plain member's) on the
    cooperative's active register, counted once per person."""
    from accounts.models import Membership, Role

    memberships = (
        Membership.all_objects
        .filter(cooperative=cooperative, status=Membership.Status.ACTIVE,
                role__isnull=False)
        .exclude(role__slug=Role.MEMBER)
        .select_related("role")
    )
    return {m.user_id for m in memberships if m.role.is_officer}


def is_sole_secretary(cooperative, user) -> bool:
    """Whether ``user`` is the Secretary and the cooperative's only officer.

    Small societies, often in remote places, are sometimes run by one person.
    Dual control cannot work there — there is no second officer to check — and
    insisting on it left every loan, withdrawal and bank change stuck forever.
    So the sole Secretary may approve their own requests, and this stops being
    true the moment a second officer is added.
    """
    from accounts.models import Membership, Role

    if not (user and getattr(user, "id", None)):
        return False
    holds_secretary = Membership.all_objects.filter(
        cooperative=cooperative, user=user,
        status=Membership.Status.ACTIVE, role__slug=Role.SECRETARY,
    ).exists()
    return holds_secretary and officer_user_ids(cooperative) == {user.id}


def _resolve(cooperative, action, object_id):
    """Return the target object + a human summary for an action."""
    from approvals.models import ApprovalRequest
    from dividends.models import DividendDeclaration
    from loans.models import Loan

    if action in (ApprovalRequest.Action.LOAN_APPROVE,
                  ApprovalRequest.Action.LOAN_DISBURSE):
        loan = Loan.all_objects.filter(cooperative=cooperative,
                                       id=object_id).first()
        if loan is None:
            raise ApprovalError("Loan not found.")
        return loan, f"Loan #{loan.id} · {loan.membership.member_no} · " \
                     f"{loan.principal}"
    if action == ApprovalRequest.Action.DIVIDEND_POST:
        dec = DividendDeclaration.all_objects.filter(
            cooperative=cooperative, id=object_id).first()
        if dec is None:
            raise ApprovalError("Dividend declaration not found.")
        if dec.status != DividendDeclaration.Status.DRAFT:
            raise ApprovalError(
                f"The {dec.period_label} dividend is "
                f"{dec.get_status_display().lower()}, not a draft.")
        return dec, (f"Dividend {dec.period_label} · {dec.total_amount} "
                     f"across {dec.allocations.count()} members")
    if action == ApprovalRequest.Action.COOP_BANK_UPDATE:
        from tenants.models import BankDetailChange

        change = BankDetailChange.all_objects.filter(
            cooperative=cooperative, id=object_id).first()
        if change is None:
            raise ApprovalError("Proposed bank details not found.")
        if change.is_applied:
            raise ApprovalError("Those bank details have already been applied.")
        # The summary is the only description the Approvals page shows, so the
        # actual old and new values go in it. Approving a change you cannot see
        # is worse than having no control at all.
        return change, change.describe()
    if action == ApprovalRequest.Action.SAVINGS_WITHDRAW:
        from savings.models import Withdrawal

        withdrawal = Withdrawal.all_objects.filter(
            cooperative=cooperative, id=object_id).first()
        if withdrawal is None:
            raise ApprovalError("Withdrawal not found.")
        if withdrawal.is_paid:
            raise ApprovalError("That withdrawal has already been paid.")
        return withdrawal, withdrawal.describe()
    raise ApprovalError("Unknown action.")


def submit_request(*, cooperative, action, object_id, requested_by, note=""):
    from approvals.models import ApprovalRequest

    _, summary = _resolve(cooperative, action, object_id)
    request_obj = ApprovalRequest.objects.create(
        cooperative=cooperative, action=action, object_id=object_id,
        summary=summary, requested_by=requested_by, note=note)

    # Maker-checker only works if the checker knows they are needed. Sent after
    # commit so a mail failure cannot undo the submitted request.
    transaction.on_commit(lambda: _alert_officers(request_obj))
    return request_obj


def _alert_officers(request_obj):
    from communications.receipts import notify_officers_by_email

    sole = is_sole_secretary(request_obj.cooperative, request_obj.requested_by)
    notify_officers_by_email(
        request_obj.cooperative,
        subject=f"Approval needed: {request_obj.get_action_display()}",
        heading="An action is waiting for approval",
        intro=("You are the cooperative's only officer, so you may approve "
               "this yourself. Once another officer is added, a different "
               "officer will have to decide requests like this one."
               if sole else
               "Segregation of duties applies — whoever submitted this cannot "
               "approve it, so another officer must decide."),
        rows=[
            ("Action", request_obj.get_action_display()),
            ("Details", request_obj.summary or "—"),
            ("Submitted by", getattr(request_obj.requested_by, "full_name",
                                     "—")),
            ("Note", request_obj.note or "—"),
        ],
    )


@transaction.atomic
def decide_request(request_obj, *, actor, approve):
    from approvals.models import ApprovalRequest

    if request_obj.status != ApprovalRequest.Status.PENDING:
        raise ApprovalError("This request has already been decided.")
    # Segregation of duties: the checker cannot be the maker — unless the
    # maker is the Secretary running the society alone (see is_sole_secretary).
    self_decided = bool(request_obj.requested_by_id and actor
                        and request_obj.requested_by_id == actor.id)
    if self_decided and not is_sole_secretary(request_obj.cooperative, actor):
        raise ApprovalError("A different officer must approve this request.")

    # What the action itself reported, for the approver to see — e.g. a loan
    # approved but not paid because the wallet is short. Transient, like the
    # loan's own auto_disbursement_error: the ledger is the durable record.
    request_obj.outcome = None
    if approve:
        request_obj.outcome = _execute(request_obj, actor)
        request_obj.status = ApprovalRequest.Status.APPROVED
    else:
        request_obj.status = ApprovalRequest.Status.REJECTED
    request_obj.decided_by = actor
    request_obj.decided_at = timezone.now()
    request_obj.save(update_fields=["status", "decided_by", "decided_at",
                                    "updated_at"])
    if self_decided:
        from audit.services import record_action

        # Nobody else checked this one, so the log must say so plainly.
        record_action(
            cooperative=request_obj.cooperative, actor=actor,
            action="approval.self_decided", entity=request_obj,
            after={"status": request_obj.status,
                   "reason": "sole officer (Secretary)"},
        )
    return request_obj


def _execute(request_obj, actor):
    from approvals.models import ApprovalRequest

    target, _ = _resolve(request_obj.cooperative, request_obj.action,
                         request_obj.object_id)
    if request_obj.action == ApprovalRequest.Action.LOAN_APPROVE:
        from loans.services import approve_loan
        loan = approve_loan(target, actor=actor, approve=True)
        if loan.auto_disbursement_error:
            return {"message": loan.auto_disbursement_error,
                    "wallet_shortfall": loan.auto_disbursement_shortfall}
        if loan.auto_disbursement is not None:
            return {"message": "Approved and paid to the member's bank.",
                    "wallet_shortfall": None}
    elif request_obj.action == ApprovalRequest.Action.LOAN_DISBURSE:
        from loans.services import disburse_loan
        disburse_loan(target, actor=actor)
    elif request_obj.action == ApprovalRequest.Action.DIVIDEND_POST:
        from dividends.services import DividendError, post_dividend
        try:
            post_dividend(target, actor=actor)
        except DividendError as exc:
            # E.g. the surplus no longer covers it: the approver is told why,
            # and the request stays pending rather than half-applied.
            raise ApprovalError(str(exc)) from exc
    elif request_obj.action == ApprovalRequest.Action.COOP_BANK_UPDATE:
        from tenants.services import apply_bank_detail_change
        apply_bank_detail_change(target, actor=actor)
    elif request_obj.action == ApprovalRequest.Action.SAVINGS_WITHDRAW:
        # The balance is re-checked inside pay_withdrawal, not at request time:
        # a member's balance can fall between proposal and approval, and two
        # pending withdrawals could otherwise overdraw them together.
        from savings.services import pay_withdrawal
        pay_withdrawal(target, actor=actor)
