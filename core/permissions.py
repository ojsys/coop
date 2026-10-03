"""
Shared DRF permissions.

The two classes here are the single home for one check: platform admin, or a
member of the active cooperative holding a privileged role. Five near-identical
copies of it used to live in ``accounts.views`` (CanManageMembers,
CanManageRoles), ``accounts.join_views`` and ``approvals.views`` (both
IsPrivilegedMember) and ``savings.views`` (CanPayOutSavings). They differed on
exactly one axis — whether reads were exempt — which is now the choice between
``IsPrivilegedOfficer`` and ``IsPrivilegedOfficerOrReadOnly``.

Choose between them deliberately. Putting the read-only variant on an endpoint
that should be officers-only (join requests, approvals) would open it to any
authenticated member, which is the kind of widening a consolidation like this
can introduce silently.

None of these check a *specific* permission such as ``members.manage``; they all
test ``Role.is_privileged``, i.e. whether the role holds any of
``Role.PRIVILEGED_PERMISSIONS``. If an endpoint ever needs a narrower grant, it
wants a new class here rather than a sixth hand-rolled copy.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework import permissions

from core.tenancy import resolve_cooperative


def is_privileged_officer(user, request=None, *, requested_id=None) -> bool:
    """Whether ``user`` may take privileged action in the active cooperative.

    Resolves the tenant itself rather than reading the bound one:
    ``TenantScopedViewMixin`` binds it in ``initial()``, which runs *after*
    permission checks, so a permission class that trusted the ambient tenant
    would be reading ``None`` on every request.
    """
    from accounts.models import Membership

    if not (user and user.is_authenticated):
        return False
    if user.is_platform_admin:
        return True

    if requested_id is None and request is not None:
        requested_id = request.META.get(settings.TENANT_HEADER)
    cooperative = resolve_cooperative(user, requested_id)
    if cooperative is None:
        return False

    return any(
        m.role and m.role.is_privileged
        for m in Membership.all_objects
        .filter(user=user, cooperative=cooperative)
        .select_related("role")
    )


class IsPrivilegedOfficer(permissions.BasePermission):
    """Only a privileged officer of the active cooperative (or a platform admin)."""

    def has_permission(self, request, view):
        return is_privileged_officer(request.user, request)


class IsPrivilegedOfficerOrReadOnly(permissions.BasePermission):
    """Reads open to any authenticated member; writes to privileged officers.

    The read/write split matters for records members have a legitimate interest
    in seeing but no business creating — their own payouts, the cooperative's
    ledger movements.
    """

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_privileged_officer(user, request)
