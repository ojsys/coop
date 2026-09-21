from __future__ import annotations

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from core.views import TenantScopedViewMixin
from savings.models import SavingsGoal, SavingsProduct
from savings.serializers import SavingsGoalSerializer, SavingsProductSerializer


class SavingsProductViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = SavingsProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsProduct.objects.select_related("contribution_type")


class SavingsGoalViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = SavingsGoalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = SavingsGoal.objects.select_related("product", "membership")
        membership = self.request.query_params.get("membership")
        if membership:
            qs = qs.filter(membership_id=membership)
        return qs
