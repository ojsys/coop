"""Submitting and deciding maker-checker approval requests."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone


class ApprovalError(Exception):
    """An approval request could not be submitted or decided."""


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
        return dec, f"Dividend {dec.period_label} · {dec.total_amount}"
    raise ApprovalError("Unknown action.")


def submit_request(*, cooperative, action, object_id, requested_by, note=""):
    from approvals.models import ApprovalRequest

    _, summary = _resolve(cooperative, action, object_id)
    return ApprovalRequest.objects.create(
        cooperative=cooperative, action=action, object_id=object_id,
        summary=summary, requested_by=requested_by, note=note)


@transaction.atomic
def decide_request(request_obj, *, actor, approve):
    from approvals.models import ApprovalRequest

    if request_obj.status != ApprovalRequest.Status.PENDING:
        raise ApprovalError("This request has already been decided.")
    # Segregation of duties: the checker cannot be the maker.
    if request_obj.requested_by_id and actor \
            and request_obj.requested_by_id == actor.id:
        raise ApprovalError("A different officer must approve this request.")

    if approve:
        _execute(request_obj, actor)
        request_obj.status = ApprovalRequest.Status.APPROVED
    else:
        request_obj.status = ApprovalRequest.Status.REJECTED
    request_obj.decided_by = actor
    request_obj.decided_at = timezone.now()
    request_obj.save(update_fields=["status", "decided_by", "decided_at",
                                    "updated_at"])
    return request_obj


def _execute(request_obj, actor):
    from approvals.models import ApprovalRequest

    target, _ = _resolve(request_obj.cooperative, request_obj.action,
                         request_obj.object_id)
    if request_obj.action == ApprovalRequest.Action.LOAN_APPROVE:
        from loans.services import approve_loan
        approve_loan(target, actor=actor, approve=True)
    elif request_obj.action == ApprovalRequest.Action.LOAN_DISBURSE:
        from loans.services import disburse_loan
        disburse_loan(target, actor=actor)
    elif request_obj.action == ApprovalRequest.Action.DIVIDEND_POST:
        from dividends.services import post_dividend
        post_dividend(target, actor=actor)
