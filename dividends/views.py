from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.context import get_current_cooperative
from core.views import TenantScopedViewMixin
from dividends.models import DividendDeclaration
from dividends.serializers import DividendDeclarationSerializer
from dividends.services import declare_dividend, post_dividend


class DividendViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                      mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                      viewsets.GenericViewSet):
    """Declare, preview, post and review dividend distributions."""

    serializer_class = DividendDeclarationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return DividendDeclaration.objects.prefetch_related("allocations")

    def create(self, request, *args, **kwargs):
        """Declare a distribution: allocates pro-rata to share capital (DRAFT)."""
        total = request.data.get("total_amount")
        period = request.data.get("period_label")
        if not total or not period:
            return Response(
                {"detail": "total_amount and period_label are required."},
                status=400)
        declaration = declare_dividend(
            cooperative=get_current_cooperative(), total_amount=total,
            period_label=period, note=request.data.get("note", ""),
            created_by=request.user)
        return Response(self.get_serializer(declaration).data, status=201)

    @action(detail=True, methods=["post"])
    def post_to_ledger(self, request, pk=None):
        declaration = self.get_object()
        try:
            post_dividend(declaration, actor=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(declaration).data)
