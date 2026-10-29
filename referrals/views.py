"""
Refer and earn — the endpoints.

Public:
* ``GET  /public/referral-program/`` — the terms, for the signup page.
* ``POST /public/refer/``            — sign up to refer (no cooperative).

The signed-in referrer:
* ``GET   /me/referrer/``            — code, balance, referrals, history.
* ``POST  /me/referrer/``            — turn referrals on for this account.
* ``PATCH /me/referrer/``            — bank details for payouts.
* ``POST  /me/referrer/payouts/``    — ask for (part of) the balance.

The platform:
* ``/platform-referrers/``           — referrers; suspend, reactivate, and
  credit a cooperative to one (``attach``).
* ``/platform-referral-payouts/``    — requests; mark paid or reject.
* ``/platform-commissions/``         — what each paid invoice earned; void.
* ``/platform-referral-program/``    — the terms.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.pagination import StandardPagination
from core.public_views import PublicView
from platform_admin.views import IsPlatformAdmin
from referrals import services
from referrals.models import (Commission, Referral, ReferralPayout,
                              ReferralProgram, Referrer)

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def _error(exc):
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def referral_link(code: str) -> str:
    return f"{settings.SITE_URL.rstrip('/')}/signup?ref={code}"


# ── Serializers ────────────────────────────────────────────────────────────
class ProgramSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferralProgram
        fields = ["active", "commission_percent", "commission_months",
                  "minimum_payout", "updated_at"]
        read_only_fields = ["updated_at"]


class ReferralSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(source="cooperative.name",
                                             read_only=True)
    cooperative_state = serializers.CharField(source="cooperative.state",
                                              read_only=True)
    subscription_status = serializers.SerializerMethodField()
    window_ends_on = serializers.SerializerMethodField()
    earned = serializers.DecimalField(max_digits=14, decimal_places=2,
                                      read_only=True)

    class Meta:
        model = Referral
        fields = ["id", "cooperative", "cooperative_name", "cooperative_state",
                  "subscription_status", "commission_percent",
                  "commission_months", "first_paid_on", "window_ends_on",
                  "earned", "source", "created_at"]

    def get_subscription_status(self, obj) -> str:
        sub = getattr(obj.cooperative, "subscription", None)
        return sub.status if sub else ""

    def get_window_ends_on(self, obj):
        return services.window_end(obj)


class CommissionSerializer(serializers.ModelSerializer):
    cooperative_name = serializers.CharField(
        source="referral.cooperative.name", read_only=True)
    invoice_number = serializers.CharField(source="invoice.number",
                                           read_only=True)
    paid_at = serializers.DateField(source="invoice.paid_at", read_only=True)
    referrer_name = serializers.CharField(
        source="referral.referrer.user.full_name", read_only=True)

    class Meta:
        model = Commission
        fields = ["id", "cooperative_name", "invoice_number", "paid_at",
                  "invoice_amount", "percent", "amount", "status",
                  "void_reason", "referrer_name", "created_at"]


class PayoutSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    referrer_name = serializers.CharField(source="referrer.user.full_name",
                                          read_only=True)
    referrer_email = serializers.CharField(source="referrer.user.email",
                                           read_only=True)

    class Meta:
        model = ReferralPayout
        fields = ["id", "amount", "bank_name", "bank_code", "account_no",
                  "account_name", "status", "status_display", "reference",
                  "note", "decided_at", "referrer_name", "referrer_email",
                  "created_at"]


def _with_earned(referrals):
    return referrals.select_related("cooperative__subscription").annotate(
        earned=Coalesce(
            Sum("commissions__amount",
                filter=Q(commissions__status=Commission.Status.EARNED)),
            Value(0), output_field=_MONEY))


# ── Public ─────────────────────────────────────────────────────────────────
class PublicProgramView(PublicView):
    def get(self, request):
        return Response(ProgramSerializer(ReferralProgram.load()).data)


class ReferrerSignupSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20, required=False,
                                  allow_blank=True, default="")
    password = serializers.CharField(write_only=True, max_length=128,
                                     trim_whitespace=False)

    def validate(self, attrs):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoError

        from accounts.models import User

        if not User.objects.filter(email__iexact=attrs["email"]).exists():
            try:
                validate_password(attrs["password"],
                                  User(email=attrs["email"],
                                       full_name=attrs["full_name"]))
            except DjangoError as exc:
                raise serializers.ValidationError(
                    {"password": list(exc.messages)}) from exc
        return attrs


class ReferrerSignupView(PublicView):
    """`POST /public/refer/` — sign up to refer cooperatives.

    No token in the response: the client signs in through ``/auth/token/``
    with the password just chosen, as society signup does.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        serializer = ReferrerSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            referrer = services.sign_up_referrer(**serializer.validated_data)
        except services.ReferralError as exc:
            return _error(exc)
        return Response({"detail": "You're ready to refer.",
                         "code": referrer.code},
                        status=status.HTTP_201_CREATED)


# ── The signed-in referrer ─────────────────────────────────────────────────
class BankDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Referrer
        fields = ["bank_name", "bank_code", "account_no", "account_name"]

    def validate_account_no(self, value):
        value = value.strip()
        if value and not (value.isdigit() and len(value) == 10):
            raise serializers.ValidationError(
                "A Nigerian account number is 10 digits.")
        return value


class MyReferrerView(APIView):
    permission_classes = [IsAuthenticated]

    def _dashboard(self, request, referrer):
        program = ReferralProgram.load()
        totals = services.totals(referrer)
        referrals = _with_earned(referrer.referrals.all())
        commissions = (Commission.objects
                       .filter(referral__referrer=referrer)
                       .select_related("referral__cooperative", "invoice",
                                       "referral__referrer__user")[:50])
        payouts = referrer.payouts.select_related("referrer__user")[:50]
        return Response({
            "code": referrer.code,
            "link": referral_link(referrer.code),
            "status": referrer.status,
            "program": ProgramSerializer(program).data,
            "totals": {k: f"{v:.2f}" for k, v in totals.items()},
            "bank": BankDetailsSerializer(referrer).data,
            "has_bank_details": referrer.has_bank_details,
            "referrals": ReferralSerializer(referrals, many=True).data,
            "commissions": CommissionSerializer(commissions, many=True).data,
            "payouts": PayoutSerializer(payouts, many=True).data,
        })

    def get(self, request):
        referrer = Referrer.objects.filter(user=request.user).first()
        if referrer is None:
            return Response({"detail": "Not a referrer."},
                            status=status.HTTP_404_NOT_FOUND)
        return self._dashboard(request, referrer)

    def post(self, request):
        try:
            referrer = services.become_referrer(request.user)
        except services.ReferralError as exc:
            return _error(exc)
        return self._dashboard(request, referrer)

    def patch(self, request):
        referrer = Referrer.objects.filter(user=request.user).first()
        if referrer is None:
            return Response({"detail": "Not a referrer."},
                            status=status.HTTP_404_NOT_FOUND)
        serializer = BankDetailsSerializer(referrer, data=request.data,
                                           partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return self._dashboard(request, referrer)


class MyPayoutRequestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        referrer = Referrer.objects.filter(user=request.user).first()
        if referrer is None:
            return Response({"detail": "Not a referrer."},
                            status=status.HTTP_404_NOT_FOUND)
        amount = serializers.DecimalField(
            max_digits=12, decimal_places=2).run_validation(
                request.data.get("amount"))
        try:
            payout = services.request_payout(referrer, amount=amount)
        except services.ReferralError as exc:
            return _error(exc)
        return Response(PayoutSerializer(payout).data,
                        status=status.HTTP_201_CREATED)


# ── The platform ───────────────────────────────────────────────────────────
class PlatformReferrerSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="user.full_name", read_only=True)
    email = serializers.CharField(source="user.email", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    referral_count = serializers.IntegerField(read_only=True)
    earned = serializers.DecimalField(max_digits=14, decimal_places=2,
                                      read_only=True)
    totals = serializers.SerializerMethodField()

    class Meta:
        model = Referrer
        fields = ["id", "code", "status", "full_name", "email", "phone",
                  "bank_name", "account_no", "account_name",
                  "referral_count", "earned", "totals", "created_at"]

    def get_totals(self, obj):
        return {k: f"{v:.2f}" for k, v in services.totals(obj).items()}


class PlatformReferrerViewSet(mixins.ListModelMixin,
                              mixins.RetrieveModelMixin,
                              viewsets.GenericViewSet):
    serializer_class = PlatformReferrerSerializer
    permission_classes = [IsPlatformAdmin]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = (Referrer.objects.select_related("user")
              .annotate(referral_count=Count("referrals", distinct=True),
                        earned=Coalesce(
                            Sum("referrals__commissions__amount",
                                filter=Q(referrals__commissions__status=(
                                    Commission.Status.EARNED))),
                            Value(0), output_field=_MONEY))
              .order_by("-created_at"))
        q = (self.request.query_params.get("search") or "").strip()
        if q:
            qs = qs.filter(Q(user__full_name__icontains=q)
                           | Q(user__email__icontains=q)
                           | Q(code__iexact=q))
        return qs

    def retrieve(self, request, *args, **kwargs):
        referrer = self.get_object()
        data = self.get_serializer(referrer).data
        data["referrals"] = ReferralSerializer(
            _with_earned(referrer.referrals.all()), many=True).data
        return Response(data)

    def _set_status(self, new):
        referrer = self.get_object()
        referrer.status = new
        referrer.save(update_fields=["status", "updated_at"])
        from audit.services import record_action
        record_action(actor=self.request.user,
                      action=f"referral.referrer.{new}", entity=referrer)
        return Response(self.get_serializer(
            self.get_queryset().get(pk=referrer.pk)).data)

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        return self._set_status(Referrer.Status.SUSPENDED)

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        return self._set_status(Referrer.Status.ACTIVE)

    @action(detail=True, methods=["post"])
    def attach(self, request, pk=None):
        """Credit a cooperative to this referrer after it signed up."""
        from tenants.models import Cooperative

        coop = Cooperative.objects.filter(
            pk=request.data.get("cooperative")).first()
        if coop is None:
            return _error("Choose a cooperative.")
        try:
            services.attach_by_platform(coop, referrer=self.get_object(),
                                        actor=request.user)
        except services.ReferralError as exc:
            return _error(exc)
        return self.retrieve(request, pk=pk)


class PlatformPayoutViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = PayoutSerializer
    permission_classes = [IsPlatformAdmin]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = ReferralPayout.objects.select_related("referrer__user")
        wanted = self.request.query_params.get("status")
        return qs.filter(status=wanted) if wanted else qs

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        try:
            payout = services.mark_payout_paid(
                self.get_object(), actor=request.user,
                reference=request.data.get("reference") or "")
        except services.ReferralError as exc:
            return _error(exc)
        return Response(self.get_serializer(payout).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        try:
            payout = services.reject_payout(
                self.get_object(), actor=request.user,
                note=request.data.get("note") or "")
        except services.ReferralError as exc:
            return _error(exc)
        return Response(self.get_serializer(payout).data)


class PlatformCommissionViewSet(mixins.ListModelMixin,
                                viewsets.GenericViewSet):
    serializer_class = CommissionSerializer
    permission_classes = [IsPlatformAdmin]
    pagination_class = StandardPagination

    def get_queryset(self):
        return Commission.objects.select_related(
            "referral__cooperative", "referral__referrer__user", "invoice")

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return _error("Say why it is being voided.")
        commission = services.void_commission(self.get_object(),
                                              reason=reason,
                                              actor=request.user)
        return Response(self.get_serializer(commission).data)


class PlatformProgramView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response(ProgramSerializer(ReferralProgram.load()).data)

    def patch(self, request):
        program = ReferralProgram.load()
        serializer = ProgramSerializer(program, data=request.data,
                                       partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        from audit.services import record_action
        record_action(actor=request.user, action="referral.program.update",
                      entity=program, after=serializer.data)
        return Response(serializer.data)
