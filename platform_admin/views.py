from __future__ import annotations

from django.db.models import Count
from django.utils import timezone
from rest_framework import mixins, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from platform_admin.models import (
    Domain, Incident, Invoice, NotificationTemplate, OnboardingItem, Plan,
    PlatformTeamMember, ProviderCheck, ProviderStatus, Subscription,
    SupportTicket,
)
from platform_admin.serializers import (
    DomainSerializer, IncidentSerializer, InvoiceSerializer,
    NotificationTemplateSerializer, OnboardingItemSerializer, PlanSerializer,
    PlatformTeamMemberSerializer, PlatformUserPasswordSerializer,
    PlatformUserSerializer, ProviderStatusSerializer, SubscriptionSerializer,
    SupportTicketSerializer,
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
        # Routed through settle_invoice so marking a bank transfer paid does
        # exactly what a card payment does: rolls the billing period on, makes
        # the subscription ACTIVE, and lifts a suspension. Setting the status
        # here directly left the subscription untouched, so a paid cooperative
        # stayed PAST_DUE and was re-invoiced for a period it had settled.
        from platform_admin.billing import settle_invoice

        invoice = settle_invoice(self.get_object())
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


class PlatformUserViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Every account on the platform, for staff helping people who cannot help
    themselves: a misspelt name, a wrong email, a forgotten password with no
    access to the inbox the reset link goes to.

    Each change is written to the audit log with the staff member who made it.
    A password is never recorded — only that it was set, and by whom.
    """

    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformUserSerializer
    # Fields whose before/after go in the audit entry for an edit.
    AUDITED_FIELDS = ("full_name", "email", "phone")

    def get_queryset(self):
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        qs = get_user_model().objects.all()
        q = (self.request.query_params.get("search") or "").strip()
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(email__icontains=q)
                           | Q(phone__icontains=q)
                           | Q(memberships__member_no__icontains=q)).distinct()
        return qs.order_by("full_name", "id")

    def _guard(self, user):
        """Platform staff may not change a superuser's account unless they are
        one: otherwise support access is a route to the keys of the platform."""
        if user.is_superuser and not self.request.user.is_superuser:
            raise PermissionDenied(
                "Only a superuser can change a superuser's account.")

    def perform_update(self, serializer):
        from audit.services import record_action

        user = serializer.instance
        self._guard(user)
        before = {f: getattr(user, f) for f in self.AUDITED_FIELDS}
        user = serializer.save()
        after = {f: getattr(user, f) for f in self.AUDITED_FIELDS}
        changed = [f for f in self.AUDITED_FIELDS if before[f] != after[f]]
        if changed:
            record_action(
                actor=self.request.user, action="platform.user_updated",
                entity=user,
                before={f: before[f] for f in changed},
                after={f: after[f] for f in changed},
            )

    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        """Set a new password for someone, and sign them out everywhere.

        Staff then pass the password on to the person by a channel they trust;
        it is not emailed, because the usual reason for being here is that the
        person cannot get at their email.
        """
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError
        from rest_framework.authtoken.models import Token

        from audit.services import record_action

        user = self.get_object()
        self._guard(user)
        serializer = PlatformUserPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        password = serializer.validated_data["password"]
        try:
            validate_password(password, user)
        except DjangoValidationError as exc:
            return Response({"password": list(exc.messages)}, status=400)

        user.set_password(password)
        user.save(update_fields=["password", "updated_at"])
        Token.objects.filter(user=user).delete()
        record_action(actor=request.user,
                      action="platform.user_password_set", entity=user)
        return Response({"detail": f"Password set for {user.full_name}. "
                                   "They have been signed out everywhere."})

    @action(detail=True, methods=["post"], url_path="send-reset-link")
    def send_reset_link(self, request, pk=None):
        """Email the person the same reset link "Forgot password" sends."""
        from audit.services import record_action
        from communications.email import send_password_reset_email

        user = self.get_object()
        self._guard(user)
        sent = send_password_reset_email(user)
        record_action(actor=request.user,
                      action="platform.user_reset_link_sent", entity=user,
                      after={"email": user.email, "sent": bool(sent)})
        if not sent:
            return Response({"detail": "The reset email could not be sent."},
                            status=502)
        return Response({"detail": f"Reset link sent to {user.email}."})

    @action(detail=False, methods=["get"])
    def activity(self, request):
        """The audit trail for account changes.

        With ``?user=<id>``, everything recorded against that one account —
        including the person's own password changes and resets. Without it,
        every change platform staff have made to anyone's account.
        """
        from audit.models import AuditLog
        from audit.serializers import AuditLogSerializer

        qs = AuditLog.all_objects.select_related("actor")
        user_id = request.query_params.get("user")
        if user_id:
            qs = qs.filter(entity_type="User", entity_id=str(user_id))
        else:
            qs = qs.filter(action__startswith="platform.user_")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(
            AuditLogSerializer(page, many=True).data)
