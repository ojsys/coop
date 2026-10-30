"""
`POST /subscription/renew/` — an officer renewing from the console.

Returns the open invoice's pay token, raising the invoice first if the daily
cycle has not yet done so. Exempt from the lapse gate in ``core.permissions``
on purpose: renewing is the one thing a lapsed society must still be able to do.
"""
from __future__ import annotations

from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.context import get_current_cooperative
from core.permissions import is_privileged_officer
from core.views import TenantScopedViewMixin
from platform_admin.billing import (
    UnpricedSubscription, recheck_invoice, renewal_invoice,
)


class CanRenew(permissions.BasePermission):
    def has_permission(self, request, view):
        return is_privileged_officer(request.user, request)


class SubscriptionRenewView(TenantScopedViewMixin, APIView):
    permission_classes = [CanRenew]

    def post(self, request):
        cooperative = get_current_cooperative()
        try:
            invoice = renewal_invoice(cooperative)
        except UnpricedSubscription as exc:
            return Response(
                {"detail": f"{exc} Contact us and we will send a quote."},
                status=400)
        return Response({
            # Already paid, its confirmation late: nothing to pay.
            "paid": recheck_invoice(invoice),
            "number": invoice.number,
            "period_label": invoice.period_label,
            "amount": str(invoice.amount),
            "currency": invoice.currency,
            "due_at": invoice.due_at,
            "pay_token": invoice.pay_token,
        })
