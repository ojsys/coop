from __future__ import annotations

from rest_framework import mixins, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from django.conf import settings

from approvals.models import ApprovalRequest
from approvals.serializers import ApprovalRequestSerializer
from approvals.services import ApprovalError, decide_request, submit_request
from core.context import get_current_cooperative
from core.tenancy import resolve_cooperative
from core.views import TenantScopedViewMixin


class IsPrivilegedMember(permissions.BasePermission):
    """A platform admin, or a privileged officer of the active cooperative.

    This viewset was IsAuthenticated alone, which meant *any* member of a
    cooperative could approve a loan disbursement or a dividend posting — so
    maker-checker was enforced against the submitter only, and anyone else at
    all counted as the checker.

    Resolves the tenant itself: TenantScopedViewMixin binds it in initial(),
    which runs *after* permission checks (mirrors accounts.views.CanManageRoles).
    """

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_platform_admin:
            return True

        from accounts.models import Membership

        cooperative = resolve_cooperative(
            user, request.META.get(settings.TENANT_HEADER))
        if cooperative is None:
            return False
        return any(
            m.role and m.role.is_privileged
            for m in Membership.all_objects.filter(
                user=user, cooperative=cooperative).select_related("role")
        )


class ApprovalRequestViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                             mixins.RetrieveModelMixin,
                             viewsets.GenericViewSet):
    """Maker-checker queue. Submit a sensitive action, then a *different*
    officer approves or rejects it."""

    serializer_class = ApprovalRequestSerializer
    # Privileged officers only. With IsAuthenticated any member of the
    # cooperative counted as a valid checker, so dual control was enforced
    # against the submitter and nobody else.
    permission_classes = [IsPrivilegedMember]

    def get_queryset(self):
        qs = ApprovalRequest.objects.select_related("requested_by",
                                                    "decided_by")
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    def create(self, request, *args, **kwargs):
        try:
            req = submit_request(
                cooperative=get_current_cooperative(),
                action=request.data.get("action"),
                object_id=request.data.get("object_id"),
                requested_by=request.user,
                note=request.data.get("note", ""))
        except ApprovalError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(req).data, status=201)

    def _decide(self, request, approve):
        req = self.get_object()
        try:
            decide_request(req, actor=request.user, approve=approve)
        except ApprovalError as exc:
            return Response({"detail": str(exc)}, status=400)
        req.refresh_from_db()
        return Response(self.get_serializer(req).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._decide(request, approve=True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._decide(request, approve=False)
