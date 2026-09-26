from __future__ import annotations

from django.utils import timezone
from rest_framework import mixins, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from tenants.models import Cooperative
from tenants.serializers import (
    CooperativeSerializer, CooperativeUpdateSerializer,
)
from tenants.services import provision_cooperative


class IsPlatformAdmin(permissions.BasePermission):
    """Only Startup Ripple platform admins may provision cooperatives."""

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_platform_admin
        )


class IsCoopAdminOrPlatform(permissions.BasePermission):
    """Editing a society profile: platform admins, or a privileged member
    (secretary/treasurer/chairperson) of *that* cooperative."""

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_platform_admin:
            return True
        from accounts.models import Membership

        memberships = (
            Membership.all_objects.filter(user=user, cooperative=obj)
            .select_related("role")
        )
        return any(m.role and m.role.is_privileged for m in memberships)


class CooperativeViewSet(mixins.ListModelMixin,
                         mixins.RetrieveModelMixin,
                         mixins.CreateModelMixin,
                         mixins.UpdateModelMixin,
                         viewsets.GenericViewSet):
    """Cooperative directory & provisioning (platform) + society-profile
    editing (coop admin)."""

    serializer_class = CooperativeSerializer

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return CooperativeUpdateSerializer
        return CooperativeSerializer

    # Platform-only lifecycle / support actions.
    _PLATFORM_ACTIONS = {
        "suspend", "reactivate", "offboard", "impersonate", "health_check",
    }

    def get_permissions(self):
        if self.action == "create":
            return [IsPlatformAdmin()]
        if self.action in ("update", "partial_update"):
            return [IsCoopAdminOrPlatform()]
        if self.action in self._PLATFORM_ACTIONS:
            return [IsPlatformAdmin()]
        return super().get_permissions()

    def get_queryset(self):
        from accounts.models import Membership

        user = self.request.user
        if user.is_platform_admin:
            return Cooperative.objects.all()
        # A normal user only sees cooperatives they belong to. Use the
        # unscoped manager: this deliberately looks across tenants.
        coop_ids = (
            Membership.all_objects.filter(user=user)
            .values_list("cooperative_id", flat=True)
        )
        return Cooperative.objects.filter(id__in=coop_ids)

    def perform_create(self, serializer):
        # Platform provisioning starts a cooperative as PROSPECTIVE and drops it
        # into the onboarding pipeline; it goes ACTIVE at go-live.
        from platform_admin.services import start_onboarding

        data = serializer.validated_data
        coop = provision_cooperative(
            status=Cooperative.Status.PROSPECTIVE, **data)
        start_onboarding(coop, owner=self.request.user)
        serializer.instance = coop

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def _set_status(self, request, new_status, verb):
        from audit.services import record_action

        coop = self.get_object()
        before = coop.status
        coop.status = new_status
        coop.save(update_fields=["status", "updated_at"])
        record_action(
            cooperative=coop, actor=request.user, action=f"cooperative.{verb}",
            entity=coop, before={"status": before},
            after={"status": new_status},
        )
        return Response(CooperativeSerializer(coop).data)

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        return self._set_status(request, Cooperative.Status.SUSPENDED,
                                "suspend")

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        return self._set_status(request, Cooperative.Status.ACTIVE,
                                "reactivate")

    @action(detail=True, methods=["post"])
    def offboard(self, request, pk=None):
        return self._set_status(request, Cooperative.Status.CLOSED,
                                "offboard")

    # ── Impersonation (consent-gated, audit-logged) ─────────────────────────
    @action(detail=True, methods=["post"])
    def impersonate(self, request, pk=None):
        from accounts.models import Membership
        from audit.services import record_action
        from rest_framework.authtoken.models import Token

        coop = self.get_object()
        if not request.data.get("consent"):
            return Response(
                {"detail": "Explicit consent is required to impersonate."},
                status=400,
            )
        memberships = (
            Membership.all_objects.filter(
                cooperative=coop, status=Membership.Status.ACTIVE)
            .select_related("user", "role")
        )

        # A specific member when the platform console names one (support is
        # usually reproducing *that* person's problem), otherwise fall back to
        # any officer so the existing one-click flow still works.
        requested = request.data.get("membership")
        if requested:
            target = memberships.filter(pk=requested).first()
            if target is None:
                return Response(
                    {"detail": "That member is not an active member of this "
                               "cooperative."},
                    status=404,
                )
        else:
            target = next(
                (m for m in memberships if m.role and m.role.is_privileged),
                None,
            )
            if target is None:
                return Response(
                    {"detail": "This cooperative has no privileged official "
                               "to impersonate. Add one, or name a specific "
                               "member to impersonate."},
                    status=400,
                )

        token, _ = Token.objects.get_or_create(user=target.user)
        role_name = target.role.name if target.role else "Member"
        is_privileged = bool(target.role and target.role.is_privileged)
        record_action(
            cooperative=coop, actor=request.user, action="cooperative.impersonate",
            entity=target.user, entity_repr=target.user.full_name,
            after={"member_no": target.member_no, "role": role_name},
        )
        return Response({
            "token": token.key,
            "cooperative_id": coop.id,
            "user": {
                "full_name": target.user.full_name,
                "email": target.user.email,
                "member_no": target.member_no,
                "role": role_name,
                # Which surface to open: an ordinary member has no console,
                # so sending them there would land on a permission wall.
                "is_privileged": is_privileged,
            },
        })

    # ── Data health check (the real "dry-run") ──────────────────────────────
    @action(detail=True, methods=["get"], url_path="health-check")
    def health_check(self, request, pk=None):
        from platform_admin.services import cooperative_health

        return Response(cooperative_health(self.get_object()))


class PlatformViewSet(viewsets.ViewSet):
    """Cross-tenant analytics for the Startup Ripple platform surface.

    Not tenant-scoped — it aggregates across every cooperative and is therefore
    restricted to platform admins.
    """

    permission_classes = [IsPlatformAdmin]

    @action(detail=False, methods=["get"])
    def overview(self, request):
        from tenants.platform_service import platform_overview

        return Response(platform_overview())

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        from platform_admin.services import analytics

        try:
            months = min(max(int(request.query_params.get("months", 6)), 3), 24)
        except (TypeError, ValueError):
            months = 6
        return Response(analytics(months))

    @action(detail=False, methods=["get"], url_path="analytics/export")
    def analytics_export(self, request):
        import csv

        from django.http import HttpResponse

        from platform_admin.services import analytics_csv_rows

        try:
            months = min(max(int(request.query_params.get("months", 6)), 3), 24)
        except (TypeError, ValueError):
            months = 6
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="platform-analytics.csv"')
        writer = csv.writer(response)
        for row in analytics_csv_rows(months):
            writer.writerow(row)
        return response

    @action(detail=False, methods=["get"])
    def billing(self, request):
        from platform_admin.services import billing_summary

        return Response(billing_summary())

    @action(detail=False, methods=["get"])
    def attention(self, request):
        from platform_admin.services import attention_items

        return Response({"items": attention_items()})

    @action(detail=False, methods=["get"])
    def health(self, request):
        from platform_admin.services import health, provider_history

        payload = health()
        payload["provider_history"] = provider_history()
        return Response(payload)

    @action(detail=False, methods=["get"], url_path="fleet-health")
    def fleet_health(self, request):
        from platform_admin.services import fleet_health

        return Response(fleet_health())

    @action(detail=False, methods=["get"], url_path="payment-monitor")
    def payment_monitor(self, request):
        from platform_admin.services import payment_monitor

        return Response(payment_monitor())

    @action(detail=False, methods=["get"], url_path="audit-stream")
    def audit_stream(self, request):
        from platform_admin.services import audit_stream

        return Response({"events": audit_stream()})

    @action(detail=False, methods=["get", "patch"])
    def profile(self, request):
        from platform_admin.models import PlatformProfile
        from platform_admin.serializers import PlatformProfileSerializer

        profile = PlatformProfile.load()
        if request.method == "PATCH":
            serializer = PlatformProfileSerializer(
                profile, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
        return Response(PlatformProfileSerializer(profile).data)

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Platform-wide cooperative directory export (compliance / data)."""
        import csv

        from django.db.models import Count
        from django.http import HttpResponse

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="cooperatives-export.csv"')
        writer = csv.writer(response)
        writer.writerow(["id", "name", "slug", "state", "lga", "tier",
                         "status", "members", "created_at"])
        coops = (Cooperative.objects.all()
                 .annotate(members=Count("memberships"))
                 .order_by("id"))
        for c in coops:
            writer.writerow([c.id, c.name, c.slug, c.state, c.lga, c.tier,
                             c.status, c.members, c.created_at.date()])
        return response
