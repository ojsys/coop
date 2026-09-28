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
