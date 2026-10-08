from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.context import get_current_cooperative
from core.entitlements import DIVIDENDS, RequiresFeatureMixin
from core.permissions import IsPrivilegedOfficerOrOfficeHolderReadOnly
from core.views import TenantScopedViewMixin
from dividends.models import DividendDeclaration
from dividends.serializers import DividendDeclarationSerializer
from dividends.services import (DividendError, declare_dividend,
                                discard_dividend, distributable_surplus,
                                request_posting, reverse_dividend)


class DividendViewSet(RequiresFeatureMixin, TenantScopedViewMixin,
                      mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Declare, approve, post, reverse and review dividend distributions.

    Office-holders read; privileged officers act. This was open to any
    authenticated member, who could declare and post a distribution — moving
    the society's surplus into savings — or delete a posted one. Allocations
    list every member's share capital, which is not for every member to see
    either; a member sees their own through /me/dividends/.
    """

    # Reviewing past distributions stays open on every plan; declaring new ones
    # needs a plan that includes dividends. Reversing corrects one already
    # paid, so it is never blocked by the plan.
    required_feature = DIVIDENDS
    ungated_actions = frozenset({"reverse"})

    serializer_class = DividendDeclarationSerializer
    permission_classes = [IsPrivilegedOfficerOrOfficeHolderReadOnly]

    def get_queryset(self):
        return DividendDeclaration.objects.prefetch_related(
            "allocations__membership__user")

    def create(self, request, *args, **kwargs):
        """Declare a DRAFT distribution, by total pool or by rate on shares."""
        try:
            declaration = declare_dividend(
                cooperative=get_current_cooperative(),
                period_label=request.data.get("period_label"),
                total_amount=request.data.get("total_amount"),
                rate=request.data.get("rate"),
                note=request.data.get("note", ""),
                created_by=request.user)
        except DividendError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(declaration).data, status=201)

    def destroy(self, request, *args, **kwargs):
        try:
            discard_dividend(self.get_object())
        except DividendError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(status=204)

    @action(detail=False, methods=["get"])
    def surplus(self, request):
        """What is available to distribute, and the figures behind it."""
        data = distributable_surplus(get_current_cooperative())
        return Response({k: str(v) if not isinstance(v, int) else v
                         for k, v in data.items()})

    @action(detail=True, methods=["post"], url_path="request-posting")
    def request_posting(self, request, pk=None):
        """Send a draft for a second officer to approve; posted on approval."""
        declaration = self.get_object()
        try:
            request_posting(declaration, actor=request.user,
                            note=request.data.get("note", ""))
        except DividendError as exc:
            return Response({"detail": str(exc)}, status=400)
        declaration.refresh_from_db()
        return Response(self.get_serializer(declaration).data)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        """Undo a posted dividend by a mirror journal. A reason is required."""
        declaration = self.get_object()
        try:
            reverse_dividend(declaration, reason=request.data.get("reason", ""),
                             actor=request.user)
        except DividendError as exc:
            return Response({"detail": str(exc)}, status=400)
        declaration.refresh_from_db()
        return Response(self.get_serializer(declaration).data)
