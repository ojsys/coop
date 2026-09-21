"""
Tenant-resolution middleware.

Binds the active cooperative for the request and — crucially — always clears it
in ``finally`` so a pooled worker thread never leaks tenant state into the next
request.

For *session*-authenticated requests (Django admin, DRF browsable API) the user
is already known here and the tenant is resolved immediately. For *token*-
authenticated API requests DRF has not authenticated yet, so ``request.user`` is
still anonymous; those requests get their tenant bound later by
``core.views.TenantScopedViewMixin.initial``. The ``finally`` here still
guarantees cleanup in both cases.
"""
from __future__ import annotations

from django.conf import settings

from core.context import set_current_cooperative
from core.tenancy import resolve_cooperative


class CurrentTenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        requested_id = request.META.get(settings.TENANT_HEADER)
        set_current_cooperative(
            resolve_cooperative(getattr(request, "user", None), requested_id)
        )
        try:
            return self.get_response(request)
        finally:
            set_current_cooperative(None)
