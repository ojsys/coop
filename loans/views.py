from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import IsPrivilegedOfficer, IsPrivilegedOfficerOrReadOnly
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
    # Reads are harmless — a product catalogue is not member data. Writes are
    # not: this is where interest rates, maximum amounts and terms are set, and
    # under IsAuthenticated any member could rewrite the price of credit for
    # the whole society, or raise their own borrowing limit before applying.
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        return LoanProduct.objects.all()


class LoanViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """The officer-side loan book. Members use ``/me/loans/`` instead.

    Privileged officers only, **reads included** — not merely the write actions.
    Two separate problems were open under ``IsAuthenticated``:

    * ``approve`` and ``disburse`` were callable by any authenticated member, so
      a member could approve their own pending loan and then disburse it,
      posting a real journal that moves the principal out of Cash. The
      /approvals/ queue advertises dual control, but these direct endpoints were
      an unguarded parallel path straight past it.
    * ``LoanSerializer`` renders ``member_bank_account_no``, phone, email, share
      capital and photograph, and this queryset is scoped to the cooperative
      rather than to the caller — so listing it handed any member every other
      member's bank account number.
    """

    serializer_class = LoanSerializer
    permission_classes = [IsPrivilegedOfficer]
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
        """Approve a loan — and pay it out, where it can be paid.

        The response carries ``auto_disbursement_error`` when the money could
        not be sent, so the officer learns why at the moment they approve
        rather than discovering an unpaid approved loan days later.
        """
        loan = self.get_object()
        try:
            approve_loan(loan, actor=request.user, approve=True)
        except LoanError as exc:
            return Response({"detail": str(exc)}, status=400)

        # Read the transient attributes before refresh_from_db discards them.
        payout = getattr(loan, "auto_disbursement", None)
        error = getattr(loan, "auto_disbursement_error", None)

        loan.refresh_from_db()
        data = self.get_serializer(loan).data
        data["auto_disbursement_error"] = error
        data["payout_reference"] = payout.reference if payout else None
        return Response(data)

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
    # Privileged officers only. ``confirm`` posts a repayment to the ledger, and
    # the list shows other members' repayment claims — neither is ordinary
    # member business. Members report their own transfers through
    # /me/loans/{id}/report-transfer/.
    permission_classes = [IsPrivilegedOfficer]

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
