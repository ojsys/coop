"""DRF mixin that binds the active tenant after authentication."""
from __future__ import annotations

from django.conf import settings

from core.context import set_current_cooperative
from core.tenancy import resolve_cooperative


class TenantScopedViewMixin:
    """Bind the request's cooperative once DRF has authenticated the user.

    ``initial`` runs after authentication and permission checks are set up but
    before the handler, so ``request.user`` is the real (token) user here. The
    tenant is cleared by ``CurrentTenantMiddleware`` at request end.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        requested_id = request.META.get(settings.TENANT_HEADER)
        set_current_cooperative(
            resolve_cooperative(request.user, requested_id)
        )
