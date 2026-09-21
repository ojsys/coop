from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from approvals.models import ApprovalRequest
from approvals.serializers import ApprovalRequestSerializer
from approvals.services import ApprovalError, decide_request, submit_request
from core.context import get_current_cooperative
from core.views import TenantScopedViewMixin


class ApprovalRequestViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                             mixins.RetrieveModelMixin,
                             viewsets.GenericViewSet):
    """Maker-checker queue. Submit a sensitive action, then a *different*
    officer approves or rejects it."""

    serializer_class = ApprovalRequestSerializer
    permission_classes = [IsAuthenticated]

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
