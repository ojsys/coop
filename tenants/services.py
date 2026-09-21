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
