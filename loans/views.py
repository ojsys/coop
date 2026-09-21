from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.views import TenantScopedViewMixin
from loans.models import Loan, LoanProduct, LoanRepayment
from loans.serializers import (
    LoanProductSerializer, LoanSerializer, RepaymentClaimSerializer,
)
from loans.services import (
    LoanError, approve_loan, confirm_loan_repayment, disburse_loan,
    record_repayment, reject_repayment,
)


class LoanProductViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = LoanProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LoanProduct.objects.all()


class LoanViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = LoanSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = Loan.objects.select_related(
            "membership__user", "product",
        ).prefetch_related("instalments", "repayments")
        member = self.request.query_params.get("membership")
        status = self.request.query_params.get("status")
        if member:
            qs = qs.filter(membership_id=member)
        if status:
            qs = qs.filter(status=status)
        return qs

    def _act(self, fn, *args, **kwargs):
        loan = self.get_object()
        try:
            fn(loan, *args, **kwargs)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        loan.refresh_from_db()
        return Response(self.get_serializer(loan).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._act(approve_loan, actor=request.user, approve=True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._act(approve_loan, actor=request.user, approve=False)

    @action(detail=True, methods=["post"])
    def disburse(self, request, pk=None):
        return self._act(disburse_loan, actor=request.user)

    @action(detail=True, methods=["post"])
    def repay(self, request, pk=None):
        amount = request.data.get("amount")
        channel = request.data.get("channel", "cash")
        return self._act(record_repayment, amount=amount, actor=request.user,
                         channel=channel)


class LoanRepaymentViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                           viewsets.GenericViewSet):
    """Officer view of repayments — used to verify & confirm member-reported
    bank transfers (``?status=pending``)."""

    serializer_class = RepaymentClaimSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = LoanRepayment.objects.select_related(
            "loan__membership__user", "loan__product")
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Verify & post a reported transfer to the ledger."""
        repayment = self.get_object()
        try:
            confirm_loan_repayment(repayment, actor=request.user)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"status": "confirmed"})

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Dismiss a reported transfer that couldn't be verified."""
        repayment = self.get_object()
        try:
            reject_repayment(repayment, actor=request.user)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"status": "rejected"})
