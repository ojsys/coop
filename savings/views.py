from __future__ import annotations

from decimal import InvalidOperation

from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.context import get_current_cooperative
from core.entitlements import SAVINGS_PLANS, RequiresFeatureMixin
from core.permissions import IsPrivilegedOfficerOrReadOnly
from core.views import TenantScopedViewMixin
from savings.models import SavingsGoal, SavingsProduct, Withdrawal
from savings.serializers import (SavingsGoalSerializer,
                                SavingsProductSerializer,
                                WithdrawalSerializer)


class SavingsProductViewSet(RequiresFeatureMixin, TenantScopedViewMixin,
                            viewsets.ModelViewSet):
    required_feature = SAVINGS_PLANS
    serializer_class = SavingsProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsProduct.objects.select_related("contribution_type")


class SavingsGoalViewSet(RequiresFeatureMixin, TenantScopedViewMixin,
                         viewsets.ModelViewSet):
    required_feature = SAVINGS_PLANS
    serializer_class = SavingsGoalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = SavingsGoal.objects.select_related("product", "membership")
        membership = self.request.query_params.get("membership")
        if membership:
            qs = qs.filter(membership_id=membership)
        return qs


class WithdrawalViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                        mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Member savings payouts: requested here, approved under /approvals/.

    Creation goes through savings.services.request_withdrawal rather than the
    serializer's save(): the balance check and the approval request are the
    whole point, and a plain ModelSerializer create would skip both and post
    nothing to the ledger.
    """

    serializer_class = WithdrawalSerializer
    # Stricter than the rest of this app, which is IsAuthenticated: this
    # endpoint moves money out, and under IsAuthenticated an ordinary member
    # could request a payout to themselves. Reads stay open so a member can see
    # their own history; a member who wants to *ask* uses /me/withdrawals/.
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        qs = Withdrawal.objects.select_related(
            "membership", "membership__user", "requested_by", "journal")
        membership = self.request.query_params.get("membership")
        if membership:
            qs = qs.filter(membership_id=membership)
        if self.request.query_params.get("unpaid"):
            qs = qs.filter(paid_at__isnull=True)
        return qs

    def create(self, request, *args, **kwargs):
        from accounts.models import Membership
        from savings.services import WithdrawalError, request_withdrawal

        cooperative = get_current_cooperative()
        membership = Membership.objects.filter(
            pk=request.data.get("membership")).first()
        if membership is None:
            return Response({"membership": ["Unknown member."]}, status=400)

        try:
            withdrawal, approval = request_withdrawal(
                cooperative=cooperative,
                membership=membership,
                amount=request.data.get("amount"),
                channel=request.data.get("channel", "transfer"),
                reason=request.data.get("reason", ""),
                requested_by=request.user,
            )
        except (WithdrawalError, InvalidOperation, TypeError) as exc:
            # InvalidOperation: a non-numeric amount reaches Decimal() before
            # any of our own validation can report it.
            message = (str(exc) if isinstance(exc, WithdrawalError)
                       else "Enter a valid amount.")
            return Response({"detail": message}, status=400)

        payload = self.get_serializer(withdrawal).data
        payload["approval_id"] = approval.pk
        payload["detail"] = (
            "Recorded and sent for approval. Another officer must approve it "
            "before any money leaves.")
        return Response(payload, status=201)
