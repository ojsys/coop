"""
Shared tenant-resolution logic used by both the Django middleware (session /
browsable API) and the DRF view mixin (token auth, where the user is only known
after DRF authentication runs).

Resolution always uses *unscoped* managers — it must run before any tenant is
bound and must be able to see across cooperatives to pick the right one.
"""
from __future__ import annotations


def resolve_cooperative(user, requested_id):
    """Return the cooperative this request should act within, or ``None``.

    A non-platform user may only resolve to a cooperative they are a member of;
    a requested id they don't belong to yields ``None`` (fail closed).
    """
    from accounts.models import Membership
    from tenants.models import Cooperative

    if user is None or not getattr(user, "is_authenticated", False):
        return None

    if getattr(user, "is_platform_admin", False):
        if requested_id:
            return Cooperative.objects.filter(pk=requested_id).first()
        return None

    memberships = (
        Membership.all_objects.filter(user=user).select_related("cooperative")
    )
    if requested_id:
        membership = memberships.filter(cooperative_id=requested_id).first()
        return membership.cooperative if membership else None

    # Imply the tenant when the user belongs to exactly one cooperative.
    first_two = list(memberships[:2])
    if len(first_two) == 1:
        return first_two[0].cooperative
    return None
