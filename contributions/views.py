from __future__ import annotations

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Membership
from contributions.models import Contribution, ContributionType
from contributions.serializers import (
    ContributionSerializer, ContributionTypeSerializer,
    RecordContributionSerializer,
)
from contributions.services import (
    ContributionError, initiate_contribution, record_contribution,
    reverse_contribution,
)
from core.context import get_current_cooperative
from core.views import TenantScopedViewMixin


class ContributionTypeViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = ContributionTypeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ContributionType.objects.select_related("gl_account")


class ContributionViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """List contributions and record new ones (which post to the ledger)."""

    serializer_class = ContributionSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = Contribution.objects.select_related(
            "membership", "contribution_type",
        )
        member = self.request.query_params.get("membership")
        if member:
            qs = qs.filter(membership_id=member)
        return qs

    @action(detail=False, methods=["get"])
    def export(self, request):
        from reports.csv_export import contributions_csv

        return contributions_csv(self.get_queryset())

    @action(detail=False, methods=["post"])
    def record(self, request):
        """Record a contribution: creates the row and posts a balanced journal."""
        serializer = RecordContributionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cooperative = get_current_cooperative()

        membership = Membership.objects.filter(pk=data["membership"]).first()
        ctype = ContributionType.objects.filter(
            pk=data["contribution_type"],
        ).first()
        if membership is None or ctype is None:
            raise ValidationError("Unknown member or contribution type.")

        try:
            contribution = record_contribution(
                cooperative=cooperative,
                membership=membership,
                contribution_type=ctype,
                amount=data["amount"],
                channel=data["channel"],
                occurred_at=data.get("occurred_at"),
                psp_reference=data.get("psp_reference", ""),
                recorded_by=request.user,
            )
        except ContributionError as exc:
            raise ValidationError(str(exc))

        return Response(
            ContributionSerializer(contribution).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"])
    def initiate(self, request):
        """Start a member-initiated PSP payment: creates a PENDING contribution
        with a reference the webhook will later confirm (posts to the ledger)."""
        import uuid

        serializer = RecordContributionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cooperative = get_current_cooperative()

        membership = Membership.objects.filter(pk=data["membership"]).first()
        ctype = ContributionType.objects.filter(
            pk=data["contribution_type"],
        ).first()
        if membership is None or ctype is None:
            raise ValidationError("Unknown member or contribution type.")

        reference = data.get("psp_reference") or f"PSP-{uuid.uuid4().hex[:12]}"
        try:
            contribution = initiate_contribution(
                cooperative=cooperative, membership=membership,
                contribution_type=ctype, amount=data["amount"],
                channel=data.get("channel", "psp"), psp_reference=reference,
                recorded_by=request.user,
            )
        except ContributionError as exc:
            raise ValidationError(str(exc))

        # Create the Paystack checkout and hand its URL back to the client. The
        # client sends the payer there; on return, `verify` confirms & posts.
        from django.conf import settings as dj_settings
        from payments.providers import PaymentInitError
        from payments.services import initialize_payment

        authorization_url = None
        payment_error = None
        try:
            authorization_url = initialize_payment(
                contribution, email=membership.user.email,
                callback_url=request.data.get("callback_url"),
            )
        except PaymentInitError as exc:
            # Surface the reason (e.g. bad key / subaccount) so the member isn't
            # left guessing; the PENDING contribution stands and they can retry.
            payment_error = str(exc)

        payload = ContributionSerializer(contribution).data
        payload["authorization_url"] = authorization_url
        payload["payment_error"] = payment_error
        # For Paystack Inline (the on-page checkout modal), which the web client
        # opens with the public key.
        payload["paystack_public_key"] = dj_settings.PAYSTACK_PUBLIC_KEY or None
        payload["payer_email"] = membership.user.email
        payload["amount_kobo"] = int(contribution.amount * 100)
        # No keys configured at all → the flow is a local test stand-in.
        payload["payment_simulated"] = (
            dj_settings.PAYSTACK_SECRET_KEY.startswith("sk_test_dev")
            and not dj_settings.PAYSTACK_PUBLIC_KEY
        )
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def verify(self, request):
        """Confirm a PSP payment after the member completes Paystack checkout.

        Verifies the charge with Paystack and posts it to the ledger immediately,
        so the payment registers in the member app, the console and the platform
        at once. Idempotent and safe to call alongside the webhook.
        """
        from payments.providers import PaymentInitError
        from payments.services import verify_payment

        reference = request.data.get("reference")
        if not reference:
            raise ValidationError("A payment reference is required.")
        try:
            contribution = verify_payment(get_current_cooperative(), reference)
        except PaymentInitError as exc:
            raise ValidationError(str(exc))
        if contribution is None:
            raise ValidationError("No contribution found for this reference.")
        return Response(ContributionSerializer(contribution).data)

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        contribution = self.get_object()
        try:
            reverse_contribution(contribution, recorded_by=request.user)
        except ContributionError as exc:
            raise ValidationError(str(exc))
        return Response(ContributionSerializer(contribution).data)
