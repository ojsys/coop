from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from audit.models import AuditLog
from audit.serializers import AuditLogSerializer
from core.views import TenantScopedViewMixin


class AuditLogViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                      viewsets.GenericViewSet):
    """The cooperative's immutable audit trail (read-only)."""

    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor")
        entity = self.request.query_params.get("entity_type")
        if entity:
            qs = qs.filter(entity_type=entity)
        return qs
