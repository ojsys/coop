"""Provisioning a cooperative (Startup Ripple platform operation)."""
from __future__ import annotations

from django.db import transaction

from tenants.models import Cooperative


@transaction.atomic
def provision_cooperative(*, name: str, slug: str,
                          status: str = Cooperative.Status.ACTIVE,
                          **fields) -> Cooperative:
    """Create a cooperative and everything it needs to start operating:
    a seeded chart of accounts and the canonical set of roles.

    ``status`` defaults to ACTIVE (used by seeds/tests). The platform
    provisioning flow passes PROSPECTIVE so the cooperative enters the
    onboarding pipeline and only goes ACTIVE at go-live.
    """
    from accounts.models import Role

    coop = Cooperative.objects.create(
        name=name, slug=slug, status=status, **fields,
    )
    coop.seed_chart_of_accounts()
    Role.seed_defaults(coop)
    return coop


def set_cooperative_status(cooperative, new_status, *, verb: str, actor=None):
    """Change a cooperative's lifecycle status and record why.

    Extracted from the platform viewset so that an *automatic* suspension — the
    billing cycle acting on an unpaid invoice — writes the same audit entry as
    an admin clicking Suspend. Two code paths writing two different trails for
    the same event is how an audit log stops being answerable.
    """
    from audit.services import record_action

    before = cooperative.status
    if before == new_status:
        return cooperative

    cooperative.status = new_status
    cooperative.save(update_fields=["status", "updated_at"])
    record_action(
        cooperative=cooperative, actor=actor,
        action=f"cooperative.{verb}", entity=cooperative,
        actor_label="" if actor else "System",
        before={"status": before}, after={"status": new_status},
    )
    return cooperative


# ── Collection account: proposed by one officer, applied by another ──────────
# The bank account is the one field where a single mistaken or compromised
# officer can redirect every future payment, so it is the one field that does
# not take effect when saved. It is proposed here and written only by
# approvals._execute once a *different* privileged officer approves.
class BankDetailError(Exception):
    """A proposed change to the collection account was refused."""


def propose_bank_detail_change(cooperative, *, actor, bank_name="",
                               bank_account_name="", bank_account_no="",
                               bank_code="", note=""):
    """Record proposed collection-account details and send them for approval.

    Returns ``(change, approval_request)``. Nothing on the cooperative moves
    until the approval is granted.
    """
    from approvals.models import ApprovalRequest
    from approvals.services import submit_request
    from tenants.models import BankDetailChange

    proposed = {
        "bank_name": (bank_name or "").strip(),
        "bank_code": (bank_code or "").strip(),
        "bank_account_name": (bank_account_name or "").strip(),
        "bank_account_no": (bank_account_no or "").strip(),
    }
    # A bank code on its own is not a proposal — it identifies a bank but names
    # no account, and letting it through alone would raise an approval request
    # an officer could not meaningfully judge.
    if not any(value for field, value in proposed.items()
               if field != "bank_code"):
        raise BankDetailError(
            "Give at least a bank name and account number to propose.")

    unchanged = all(
        getattr(cooperative, field) == value
        for field, value in proposed.items()
    )
    if unchanged:
        raise BankDetailError(
            "Those are already the cooperative's bank details — nothing to "
            "approve.")

    change = BankDetailChange.all_objects.create(
        cooperative=cooperative,
        previous_bank_name=cooperative.bank_name,
        previous_bank_code=cooperative.bank_code,
        previous_bank_account_name=cooperative.bank_account_name,
        previous_bank_account_no=cooperative.bank_account_no,
        requested_by=actor,
        **proposed,
    )
    request_obj = submit_request(
        cooperative=cooperative,
        action=ApprovalRequest.Action.COOP_BANK_UPDATE,
        object_id=change.pk,
        requested_by=actor,
        note=note,
    )
    return change, request_obj


def apply_bank_detail_change(change, *, actor=None):
    """Write approved bank details onto the cooperative. Idempotent.

    Called only from approvals._execute, so reaching here means a second
    privileged officer has approved it. The before/after is audited because this
    is where money starts going somewhere new.
    """
    from django.utils import timezone

    from audit.services import record_action

    if change.is_applied:
        return change

    coop = change.cooperative
    before = {
        "bank_name": coop.bank_name,
        "bank_code": coop.bank_code,
        "bank_account_name": coop.bank_account_name,
        "bank_account_no": coop.bank_account_no,
    }
    coop.bank_name = change.bank_name
    coop.bank_code = change.bank_code
    coop.bank_account_name = change.bank_account_name
    coop.bank_account_no = change.bank_account_no
    coop.save(update_fields=["bank_name", "bank_code", "bank_account_name",
                             "bank_account_no", "updated_at"])

    change.applied_at = timezone.now()
    change.save(update_fields=["applied_at", "updated_at"])

    record_action(
        cooperative=coop, actor=actor,
        action="cooperative.bank_details_changed", entity=coop,
        actor_label="" if actor else "System",
        before=before,
        after={
            "bank_name": coop.bank_name,
            "bank_code": coop.bank_code,
            "bank_account_name": coop.bank_account_name,
            "bank_account_no": coop.bank_account_no,
        },
    )
    return change
