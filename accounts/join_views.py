"""
Signup endpoints.

Three are public (a society applying, a person asking to join, and the society
search that makes the second usable); one is for officers deciding those
requests. The public ones create rows and send mail on behalf of a stranger, so
they are throttled and validate carefully.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from accounts.join_models import JoinRequest
from accounts.join_services import (
    SignupError, apply_for_cooperative, approve_join_request,
    reject_join_request, request_to_join,
)
from accounts.models import Membership
from core.public_views import PublicView
from core.tenancy import resolve_cooperative
from core.views import TenantScopedViewMixin
from tenants.models import Cooperative


# ── Public: a society applying to join the platform ─────────────────────────
class CooperativeApplicationSerializer(serializers.Serializer):
    society_name = serializers.CharField(max_length=200)
    applicant_name = serializers.CharField(max_length=200)
    applicant_email = serializers.EmailField()
    applicant_phone = serializers.CharField(max_length=20, required=False,
                                            allow_blank=True, default="")
    country = serializers.CharField(max_length=2, required=False,
                                    allow_blank=True, default="")
    state = serializers.CharField(max_length=80, required=False,
                                  allow_blank=True, default="")
    lga = serializers.CharField(max_length=80, required=False,
                                allow_blank=True, default="")
    coop_type = serializers.CharField(max_length=100, required=False,
                                      allow_blank=True, default="")
    estimated_members = serializers.IntegerField(required=False, min_value=0,
                                                 default=0)
    message = serializers.CharField(max_length=1000, required=False,
                                    allow_blank=True, default="")


class CooperativeApplicationView(PublicView):
    """`POST /public/apply/` — a society asks to be onboarded."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        serializer = CooperativeApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        cooperative = apply_for_cooperative(**serializer.validated_data)
        return Response(
            {"detail": "Thank you — we've received your application and will "
                       "be in touch shortly.",
             "society": cooperative.name},
            status=status.HTTP_201_CREATED,
        )


# ── Public: finding a society, and asking to join it ────────────────────────
class PublicSocietySerializer(serializers.ModelSerializer):
    class Meta:
        model = Cooperative
        fields = ["id", "name", "slug", "country", "state", "lga"]


class PublicSocietySearchView(PublicView):
    """`GET /public/societies/?q=` — find the society you belong to.

    Search-only, never a full listing: a member needs to find their own
    society, but the platform's customer list is not something to hand out in
    bulk. Only ACTIVE societies appear — a prospective one cannot admit members
    yet.
    """

    MIN_QUERY = 2
    LIMIT = 20

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < self.MIN_QUERY:
            return Response([])

        societies = (
            Cooperative.objects
            .filter(status=Cooperative.Status.ACTIVE, name__icontains=query)
            .order_by("name")[:self.LIMIT]
        )
        return Response(PublicSocietySerializer(societies, many=True).data)


class JoinRequestCreateSerializer(serializers.Serializer):
    cooperative = serializers.IntegerField()
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=20, required=False,
                                  allow_blank=True, default="")
    message = serializers.CharField(max_length=500, required=False,
                                    allow_blank=True, default="")


class JoinRequestCreateView(PublicView):
    """`POST /public/join/` — ask a society to admit you."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        serializer = JoinRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        cooperative = Cooperative.objects.filter(
            pk=data.pop("cooperative"), status=Cooperative.Status.ACTIVE,
        ).first()
        if cooperative is None:
            raise serializers.ValidationError(
                {"cooperative": ["That society is not accepting requests."]})

        try:
            request_to_join(cooperative=cooperative, **data)
        except SignupError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc

        return Response(
            {"detail": "Your request has been sent. An officer of the society "
                       "will review it and you'll hear from us by email."},
            status=status.HTTP_201_CREATED,
        )


# ── Officers: deciding requests ─────────────────────────────────────────────
class IsPrivilegedMember(BasePermission):
    """A platform admin, or an officer of the cooperative in play."""

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_platform_admin:
            return True
        # The tenant isn't bound until the view's initial() runs, so resolve it
        # the same way here (mirrors accounts.views.CanManageRoles).
        cooperative = resolve_cooperative(
            user, request.META.get(settings.TENANT_HEADER))
        if cooperative is None:
            return False
        return any(
            m.role and m.role.is_privileged
            for m in Membership.all_objects.filter(
                user=user, cooperative=cooperative).select_related("role")
        )


class JoinRequestSerializer(serializers.ModelSerializer):
    decided_by_name = serializers.CharField(source="decided_by.full_name",
                                            read_only=True, default=None)
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True, default=None)

    class Meta:
        model = JoinRequest
        fields = ["id", "full_name", "email", "phone", "message", "status",
                  "decision_note", "decided_by_name", "decided_at",
                  "member_no", "created_at"]
        read_only_fields = fields


class JoinRequestViewSet(TenantScopedViewMixin,
                         mixins.ListModelMixin,
                         mixins.RetrieveModelMixin,
                         viewsets.GenericViewSet):
    """Membership requests waiting on this society's officers."""

    serializer_class = JoinRequestSerializer
    permission_classes = [IsPrivilegedMember]

    def get_queryset(self):
        qs = JoinRequest.objects.select_related("decided_by", "membership")
        state = self.request.query_params.get("status")
        return qs.filter(status=state) if state else qs

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        member_no = (request.data.get("member_no") or "").strip()
        if not member_no:
            raise serializers.ValidationError(
                {"member_no": ["A member number is required."]})

        try:
            membership = approve_join_request(
                self.get_object(), actor=request.user, member_no=member_no)
        except SignupError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc

        return Response({
            "detail": f"{membership.user.full_name} has been admitted. "
                      "They've been emailed a link to set their password.",
            "membership": membership.id,
        })

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        try:
            reject_join_request(
                self.get_object(), actor=request.user,
                note=(request.data.get("note") or "").strip())
        except SignupError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc

        return Response({"detail": "The request has been declined."})
