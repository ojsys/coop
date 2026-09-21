"""
Member self-service endpoints (`/me/…`).

Everything here is scoped to the *requesting member's own* membership in the
active cooperative — following the same pattern as ``NotificationViewSet`` — so
a member can only ever see and change their own records, never another member's.
Officer/console endpoints (``/loans/``, ``/savings-goals/`` …) stay separate.
"""
from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import MemberDocument, Membership
from accounts.serializers import MemberDocumentSerializer, MemberSelfSerializer
from core.views import TenantScopedViewMixin
from dividends.models import DividendAllocation
from dividends.serializers import MemberDividendSerializer
from loans.models import Loan, LoanProduct
from loans.serializers import LoanProductSerializer, LoanSerializer
from savings.models import SavingsGoal, SavingsProduct
from savings.serializers import SavingsGoalSerializer, SavingsProductSerializer


class MemberDocumentWriteSerializer(MemberDocumentSerializer):
    """Document serializer — membership is forced to the caller."""

    class Meta(MemberDocumentSerializer.Meta):
        read_only_fields = ["membership"]


class MemberLoanSerializer(LoanSerializer):
    """Loan serializer for the apply flow — membership is forced to the caller."""

    class Meta(LoanSerializer.Meta):
        read_only_fields = LoanSerializer.Meta.read_only_fields + ["membership"]


class MemberSavingsGoalWriteSerializer(SavingsGoalSerializer):
    """Savings goal — membership is forced to the caller."""

    class Meta(SavingsGoalSerializer.Meta):
        read_only_fields = ["membership"]


def _membership(request):
    """The requesting user's membership in the active cooperative (or None)."""
    return Membership.objects.filter(user=request.user).first()


class MemberProfileView(TenantScopedViewMixin, APIView):
    """`GET/PATCH /me/profile/` — the member's own record + KYC + headshot."""

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        membership = _membership(request)
        if membership is None:
            return Response({"detail": "No membership found."}, status=404)
        return Response(MemberSelfSerializer(membership,
                                             context={"request": request}).data)

    def patch(self, request):
        membership = _membership(request)
        if membership is None:
            return Response({"detail": "No membership found."}, status=404)
        serializer = MemberSelfSerializer(
            membership, data=request.data, partial=True,
            context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class MemberDocumentSelfViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                                mixins.CreateModelMixin,
                                mixins.DestroyModelMixin,
                                viewsets.GenericViewSet):
    """`/me/documents/` — the member's own KYC documents."""

    serializer_class = MemberDocumentWriteSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        membership = _membership(self.request)
        if membership is None:
            return MemberDocument.objects.none()
        return MemberDocument.objects.filter(membership=membership)

    def perform_create(self, serializer):
        serializer.save(membership=_membership(self.request))


class MemberLoanViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                        mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                        viewsets.GenericViewSet):
    """`/me/loans/` — the member's own loans; POST applies for a new one
    (created as ``pending`` for an officer to approve)."""

    serializer_class = MemberLoanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        if membership is None:
            return Loan.objects.none()
        return Loan.objects.filter(membership=membership).select_related(
            "product", "membership__user").prefetch_related(
            "instalments", "repayments")

    def perform_create(self, serializer):
        # A member can only apply for a loan for themselves; it stays pending.
        serializer.save(membership=_membership(self.request),
                        status=Loan.Status.PENDING)

    @action(detail=True, methods=["post"], url_path="repay-initiate")
    def repay_initiate(self, request, pk=None):
        """Start an online repayment on the member's own loan and return the
        Paystack checkout details (mirrors the contribution initiate flow)."""
        import uuid

        from django.conf import settings as dj_settings
        from loans.services import LoanError, initiate_loan_repayment
        from payments.providers import PaymentInitError
        from payments.services import initialize_loan_payment

        loan = self.get_object()
        amount = request.data.get("amount")
        reference = request.data.get("psp_reference") or \
            f"LRPY-{uuid.uuid4().hex[:12]}"
        try:
            repayment = initiate_loan_repayment(
                loan, amount=amount, reference=reference, channel="psp")
        except LoanError as exc:
            raise ValidationError(str(exc))

        authorization_url = None
        payment_error = None
        try:
            authorization_url = initialize_loan_payment(
                repayment, email=loan.membership.user.email,
                callback_url=request.data.get("callback_url"))
        except PaymentInitError as exc:
            payment_error = str(exc)

        return Response({
            "reference": reference,
            "amount": str(repayment.amount),
            "authorization_url": authorization_url,
            "payment_error": payment_error,
            "paystack_public_key": dj_settings.PAYSTACK_PUBLIC_KEY or None,
            "payer_email": loan.membership.user.email,
            "amount_kobo": int(repayment.amount * 100),
            "payment_simulated": (
                dj_settings.PAYSTACK_SECRET_KEY.startswith("sk_test_dev")
                and not dj_settings.PAYSTACK_PUBLIC_KEY),
        }, status=201)

    @action(detail=True, methods=["post"], url_path="report-transfer")
    def report_transfer(self, request, pk=None):
        """The member reports a direct bank transfer ("I have paid"). Creates a
        pending claim and notifies the society's officers to verify + confirm."""
        from loans.serializers import LoanRepaymentSerializer
        from loans.services import LoanError, report_transfer

        loan = self.get_object()
        try:
            repayment = report_transfer(
                loan, amount=request.data.get("amount"),
                reference=request.data.get("reference", ""),
                note=request.data.get("note", ""))
        except LoanError as exc:
            raise ValidationError(str(exc))
        return Response(LoanRepaymentSerializer(repayment).data, status=201)

    @action(detail=True, methods=["post"], url_path="repay-verify")
    def repay_verify(self, request, pk=None):
        """Confirm an online repayment after Paystack checkout — settles it to
        the ledger immediately. Idempotent and safe alongside the webhook."""
        from payments.providers import PaymentInitError
        from payments.services import verify_loan_payment

        loan = self.get_object()
        reference = request.data.get("reference")
        if not reference:
            raise ValidationError("A payment reference is required.")
        try:
            verify_loan_payment(loan.cooperative, reference)
        except PaymentInitError as exc:
            raise ValidationError(str(exc))
        loan.refresh_from_db()
        return Response(LoanSerializer(loan, context={"request": request}).data)


class MemberSavingsGoalViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """`/me/savings-goals/` — the member's own savings goals (full CRUD)."""

    serializer_class = MemberSavingsGoalWriteSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        if membership is None:
            return SavingsGoal.objects.none()
        return SavingsGoal.objects.filter(membership=membership).select_related(
            "product")

    def perform_create(self, serializer):
        serializer.save(membership=_membership(self.request))


class MemberDividendViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                            viewsets.GenericViewSet):
    """`/me/dividends/` — the member's own dividend allocations."""

    serializer_class = MemberDividendSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = _membership(self.request)
        if membership is None:
            return DividendAllocation.objects.none()
        return (DividendAllocation.objects
                .filter(membership=membership)
                .select_related("declaration")
                .order_by("-declaration__created_at"))


# Read-only product catalogues so a member can pick one when applying for a loan
# or creating a savings goal — without being able to create/edit products.
class MemberLoanProductViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                               viewsets.GenericViewSet):
    serializer_class = LoanProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LoanProduct.objects.filter(active=True)


class MemberSavingsProductViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                                  viewsets.GenericViewSet):
    serializer_class = SavingsProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return SavingsProduct.objects.filter(active=True)
