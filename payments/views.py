from __future__ import annotations

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.context import get_current_cooperative
from core.views import TenantScopedViewMixin
from payments.models import PaymentEvent, ProviderAccount
from payments.providers import WebhookVerificationError
from payments.serializers import (
    PaymentEventSerializer, ProviderAccountSerializer,
)
from payments.services import (
    ingest_webhook, reconcile_event, reconciliation_summary,
)


class WebhookView(APIView):
    """Inbound PSP webhook endpoint.

    Unauthenticated (PSPs don't carry our tokens) but signature-verified. The
    tenant is resolved from the settlement subaccount inside the payload, so no
    ``X-Cooperative-Id`` header is involved here.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    provider_name: str = ""

    def post(self, request, *args, **kwargs):
        try:
            event = ingest_webhook(
                provider_name=self.provider_name,
                raw_body=request.body,
                # DRF/Django header access is case-insensitive.
                headers={k.lower(): v for k, v in request.headers.items()},
            )
        except WebhookVerificationError:
            return Response({"detail": "Invalid signature."},
                            status=status.HTTP_401_UNAUTHORIZED)
        # Always 200 so the PSP does not retry a successfully received event.
        return Response({"status": event.status, "reference": event.reference})


class PaystackWebhookView(WebhookView):
    provider_name = "paystack"


class FlutterwaveWebhookView(WebhookView):
    provider_name = "flutterwave"


class ProviderAccountViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """A cooperative's PSP settlement subaccounts."""

    serializer_class = ProviderAccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ProviderAccount.objects.all()


class PaymentEventViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                          mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Settlement events + reconciliation tooling."""

    serializer_class = PaymentEventSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = PaymentEvent.objects.select_related(
            "matched_contribution__membership",
        )
        if self.request.query_params.get("exceptions") == "true":
            qs = qs.filter(status__in=[
                PaymentEvent.Status.UNMATCHED,
                PaymentEvent.Status.PARTIAL,
                PaymentEvent.Status.DUPLICATE,
            ])
        return qs

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Reconciliation health for the active cooperative's settlement cycle."""
        return Response(reconciliation_summary(get_current_cooperative()))

    @action(detail=True, methods=["post"])
    def rematch(self, request, pk=None):
        """Re-run matching for an exception (e.g. after fixing the reference)."""
        event = self.get_object()
        reconcile_event(event)
        event.refresh_from_db()
        return Response(PaymentEventSerializer(event).data)

    @action(detail=False, methods=["post"], url_path="rematch-all")
    def rematch_all(self, request):
        """Re-run matching over every current exception in one go."""
        exception_statuses = [
            PaymentEvent.Status.UNMATCHED, PaymentEvent.Status.PARTIAL,
            PaymentEvent.Status.DUPLICATE,
        ]
        events = self.get_queryset().filter(status__in=exception_statuses)
        rematched = 0
        for event in events:
            reconcile_event(event)
            rematched += 1
        return Response({
            "rematched": rematched,
            "summary": reconciliation_summary(get_current_cooperative()),
        })
