from __future__ import annotations

from django.db.models import Count
from django.utils import timezone
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from platform_admin.models import (
    Domain, Incident, Invoice, NotificationTemplate, OnboardingItem, Plan,
    PlatformTeamMember, ProviderCheck, ProviderStatus, Subscription,
    SupportTicket,
)
from platform_admin.serializers import (
    DomainSerializer, IncidentSerializer, InvoiceSerializer,
    NotificationTemplateSerializer, OnboardingItemSerializer, PlanSerializer,
    PlatformTeamMemberSerializer, ProviderStatusSerializer,
    SubscriptionSerializer, SupportTicketSerializer,
)


class IsPlatformAdmin(permissions.BasePermission):
    """Every platform-ops surface is restricted to Startup Ripple admins."""

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_platform_admin
        )


class PlatformViewSetBase(viewsets.ModelViewSet):
    """Shared base: platform-admin only, not tenant-scoped."""

    permission_classes = [IsPlatformAdmin]


class PlanViewSet(PlatformViewSetBase):
    serializer_class = PlanSerializer

    def get_queryset(self):
        return (
            Plan.objects
            .annotate(subscriber_count=Count("subscriptions"))
            .order_by("price_monthly", "name")
        )


class SubscriptionViewSet(PlatformViewSetBase):
    serializer_class = SubscriptionSerializer

    def get_queryset(self):
        return Subscription.objects.select_related("cooperative", "plan")


class InvoiceViewSet(PlatformViewSetBase):
    serializer_class = InvoiceSerializer

    def get_queryset(self):
        return Invoice.objects.select_related("cooperative")

    @action(detail=False, methods=["get"], url_path="next-number")
    def next_number(self, request):
        from platform_admin.services import next_invoice_number

        return Response({"number": next_invoice_number()})

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        invoice = self.get_object()
        invoice.status = Invoice.Status.PAID
        invoice.paid_at = timezone.now().date()
        invoice.save(update_fields=["status", "paid_at", "updated_at"])
        return Response(self.get_serializer(invoice).data)


class OnboardingItemViewSet(PlatformViewSetBase):
    serializer_class = OnboardingItemSerializer

    def get_queryset(self):
        return OnboardingItem.objects.select_related("cooperative", "owner")

    @action(detail=True, methods=["post"])
    def advance(self, request, pk=None):
        item = self.get_object()
        item.advance(actor=request.user)
        return Response(self.get_serializer(item).data)


class DomainViewSet(PlatformViewSetBase):
    serializer_class = DomainSerializer

    def get_queryset(self):
        return Domain.objects.select_related("cooperative")

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """Mark DNS verified + SSL issued.

        Stands in for the real DNS/ACME check that a background worker will run
        in production; here it flips the record to a verified state so the
        white-label flow is exercised end-to-end.
        """
        domain = self.get_object()
        domain.dns_status = Domain.DNSStatus.VERIFIED
        domain.ssl_status = Domain.SSLStatus.ISSUED
        domain.verified_at = timezone.now()
        domain.save(update_fields=["dns_status", "ssl_status", "verified_at",
                                   "updated_at"])
        return Response(self.get_serializer(domain).data)


class ProviderStatusViewSet(PlatformViewSetBase):
    serializer_class = ProviderStatusSerializer
    queryset = ProviderStatus.objects.all()

    @action(detail=True, methods=["post"])
    def record(self, request, pk=None):
        """Append a health sample using the provider's current reading (and
        optionally update the live status/latency from the request)."""
        provider = self.get_object()
        status_val = request.data.get("status", provider.status)
        latency = request.data.get("latency_ms", provider.latency_ms)
        provider.status = status_val
        provider.latency_ms = latency
        provider.checked_at = timezone.now()
        provider.save(update_fields=["status", "latency_ms", "checked_at",
                                     "updated_at"])
        ProviderCheck.objects.create(
            provider=provider, status=status_val, latency_ms=latency)
        return Response(self.get_serializer(provider).data)


class NotificationTemplateViewSet(PlatformViewSetBase):
    serializer_class = NotificationTemplateSerializer
    queryset = NotificationTemplate.objects.all()

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        """Render the template with sample placeholder values."""
        template = self.get_object()
        sample = {
            "coop_name": "Ìmọ̀lè Multipurpose CS",
            "member_name": "Ada Okonkwo",
            "amount": "₦35,000",
            "date": "12 Aug 2026",
            "platform_name": "Startup Ripple",
        }
        body = template.body
        subject = template.subject
        for key, val in sample.items():
            body = body.replace("{{" + key + "}}", val)
            subject = subject.replace("{{" + key + "}}", val)
        return Response({"subject": subject, "body": body, "sample": sample})


class IncidentViewSet(PlatformViewSetBase):
    serializer_class = IncidentSerializer
    queryset = Incident.objects.all()

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        incident = self.get_object()
        incident.status = Incident.Status.RESOLVED
        incident.resolved_at = timezone.now()
        incident.save(update_fields=["status", "resolved_at", "updated_at"])
        return Response(self.get_serializer(incident).data)


class PlatformTeamViewSet(PlatformViewSetBase):
    serializer_class = PlatformTeamMemberSerializer

    def get_queryset(self):
        return PlatformTeamMember.objects.select_related("user")


class SupportTicketViewSet(PlatformViewSetBase):
    serializer_class = SupportTicketSerializer

    def get_queryset(self):
        qs = SupportTicket.objects.select_related("cooperative", "created_by")
        cooperative = self.request.query_params.get("cooperative")
        if cooperative:
            qs = qs.filter(cooperative_id=cooperative)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        ticket = self.get_object()
        ticket.status = SupportTicket.Status.CLOSED
        ticket.closed_at = timezone.now()
        ticket.save(update_fields=["status", "closed_at", "updated_at"])
        return Response(self.get_serializer(ticket).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        ticket = self.get_object()
        ticket.status = SupportTicket.Status.OPEN
        ticket.closed_at = None
        ticket.save(update_fields=["status", "closed_at", "updated_at"])
        return Response(self.get_serializer(ticket).data)
