"""
Signup endpoints.

Four are public (a society signing up, a person asking to join, and the society
search and invite-link lookup that make the second usable); one is for officers deciding those
requests. The public ones create rows and send mail on behalf of a stranger, so
they are throttled and validate carefully.
"""
from __future__ import annotations

from django.db.models import Q
from django.http import Http404
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.identity import (
    IdentityError, find_user, identity_q, normalize_email, try_normalize_phone,
)
from accounts.join_models import JoinRequest
from accounts.models import User
from accounts.join_services import (
    SignupError, approve_join_request, reject_join_request, request_to_join,
    sign_up_cooperative,
)
from core.permissions import IsPrivilegedOfficer
from core.public_views import PublicView
from core.views import TenantScopedViewMixin
from tenants.models import Cooperative, fold


# ── Public: a society signing up to the platform ────────────────────────────
class CooperativeSignupSerializer(serializers.Serializer):
    society_name = serializers.CharField(max_length=200)
    applicant_name = serializers.CharField(max_length=200)
    applicant_email = serializers.EmailField()
    password = serializers.CharField(write_only=True, max_length=128,
                                     trim_whitespace=False)
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
    # From a referrer's link. An unknown code is ignored, never an error.
    referral_code = serializers.CharField(max_length=32, required=False,
                                          allow_blank=True, default="")

    def validate(self, attrs):
        # Django's validators, so signup holds the same bar as a reset. Only
        # for a new account: an existing one is checked against its own
        # password instead, whatever rules were in force when it was chosen.
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoError

        try:
            existing = find_user(
                email=normalize_email(attrs["applicant_email"]),
                phone=try_normalize_phone(attrs["applicant_phone"],
                                          attrs["country"] or None))
        except IdentityError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc
        if existing is None:
            candidate = User(email=attrs["applicant_email"],
                             full_name=attrs["applicant_name"])
            try:
                validate_password(attrs["password"], candidate)
            except DjangoError as exc:
                raise serializers.ValidationError(
                    {"password": list(exc.messages)}) from exc
        return attrs


class CooperativeSignupView(PublicView):
    """`POST /public/apply/` — a society signs up and is live at once.

    The path keeps its old name so clients built against it keep working. The
    response carries no token: the client signs in through ``/auth/token/``
    with the password it just chose, the one way into an account.
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        serializer = CooperativeSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            cooperative = sign_up_cooperative(**serializer.validated_data)
        except SignupError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"detail": f"{cooperative.name} is ready.",
             "society": cooperative.name,
             "cooperative_id": cooperative.id},
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
    bulk. Every ACTIVE society appears, from the day it signs up, unless the
    platform has unlisted it (``publicly_listed``). KYC is not a condition:
    it gates money movement only, and a request to join still needs an
    officer of the society to approve it.
    """

    MIN_QUERY = 2
    MIN_WORD = 3
    LIMIT = 20
    # Words nearly every society's name carries. Matching on them alone would
    # turn "Digital Cooperative" into a listing of every cooperative.
    GENERIC_WORDS = frozenset({
        "the", "and", "of", "for", "cooperative", "co-operative", "coop",
        "co-op", "society", "societies", "multipurpose", "multi-purpose",
        "limited", "ltd", "union", "association", "thrift", "credit",
        "savings", "mcs", "mpcs", "cs", "ctcs",
    })

    def get(self, request):
        query = (request.query_params.get("q") or "").strip()
        if len(query) < self.MIN_QUERY:
            return Response([])

        findable = (
            Cooperative.objects
            .filter(status=Cooperative.Status.ACTIVE, publicly_listed=True)
            .order_by("name")
        )
        # The phrase first, then every society sharing a distinctive word of
        # it. People type the name they call a society ("Digital Adashe" for
        # "Adashe"), and several can share a word ("Adashe", "Adashe Women's
        # Thrift"): show them all, with their LGA and state, and let the
        # member pick theirs.
        # Compared without accents or tone marks on either side, so "Imole"
        # finds "Ìmọ̀lè" and "Ìmọ̀lè" finds "Imole".
        query = fold(query)
        societies = list(findable.filter(
            search_name__icontains=query)[:self.LIMIT])
        words = {w for w in query.split()
                 if len(w) >= self.MIN_WORD and w not in self.GENERIC_WORDS}
        if words and len(societies) < self.LIMIT:
            any_word = Q()
            for word in words:
                any_word |= Q(search_name__icontains=word)
            societies += findable.filter(any_word).exclude(
                pk__in=[s.pk for s in societies])[:self.LIMIT - len(societies)]
        return Response(PublicSocietySerializer(societies, many=True).data)


class JoinLinkSocietySerializer(PublicSocietySerializer):
    """What an invite link shows: enough to recognise your own society."""

    logo = serializers.ImageField(read_only=True)

    class Meta(PublicSocietySerializer.Meta):
        fields = PublicSocietySerializer.Meta.fields + ["logo", "brand_color"]


class JoinLinkView(PublicView):
    """`GET /public/join/<code>/` — the society behind an invite link.

    The invite link replaces the search for members an officer sent it to. An
    unknown code, a link the officers have switched off and a society that is
    not live all answer the same 404, so the endpoint reveals nothing a search
    would not.
    """

    def get(self, request, code):
        society = Cooperative.objects.filter(
            join_code__iexact=code.strip(), join_link_enabled=True,
            status=Cooperative.Status.ACTIVE,
        ).first()
        if society is None:
            raise Http404
        return Response(JoinLinkSocietySerializer(
            society, context={"request": request}).data)


class JoinRequestCreateSerializer(serializers.Serializer):
    """A phone number, an email address, or both (request_to_join checks)."""

    cooperative = serializers.IntegerField()
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField(required=False, allow_blank=True,
                                   default="")
    phone = serializers.CharField(max_length=20, required=False,
                                  allow_blank=True, default="")
    message = serializers.CharField(max_length=500, required=False,
                                    allow_blank=True, default="")
    # Optional so app builds that predate it keep working; without it the
    # applicant sets a password from the welcome email or an access code.
    password = serializers.CharField(write_only=True, max_length=128,
                                     required=False, allow_blank=True,
                                     trim_whitespace=False)

    def validate(self, attrs):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoError

        if not attrs["email"].strip() and not attrs["phone"].strip():
            raise serializers.ValidationError(
                {"phone": ["Give a phone number or an email address."]})
        if attrs.get("password"):
            try:
                validate_password(attrs["password"],
                                  User(full_name=attrs["full_name"],
                                       email=attrs["email"] or None))
            except DjangoError as exc:
                raise serializers.ValidationError(
                    {"password": list(exc.messages)}) from exc
        else:
            attrs.pop("password", None)
        return attrs


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
                {"cooperative": ["That cooperative is not accepting "
                                 "requests."]})

        try:
            join_request = request_to_join(cooperative=cooperative, **data)
        except SignupError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc

        if join_request.password_hash:
            then = ("once they admit you, sign in with your "
                    f"{'phone number' if join_request.phone else 'email'} "
                    "and the password you chose.")
        elif join_request.email:
            then = "you'll hear from us by email."
        else:
            then = "they'll tell you when you can sign in."
        return Response(
            {"detail": "Your request has been sent. An officer of the "
                       f"cooperative will review it and {then}"},
            status=status.HTTP_201_CREATED,
        )


# ── Officers: deciding requests ─────────────────────────────────────────────
class JoinRequestSerializer(serializers.ModelSerializer):
    decided_by_name = serializers.CharField(source="decided_by.full_name",
                                            read_only=True, default=None)
    member_no = serializers.CharField(source="membership.member_no",
                                      read_only=True, default=None)
    # Someone already on CooperativeOS (a member of another society, say)
    # signs in with the account they have; approving just adds this society.
    has_account = serializers.SerializerMethodField()

    class Meta:
        model = JoinRequest
        fields = ["id", "full_name", "email", "phone", "message", "status",
                  "decision_note", "decided_by_name", "decided_at",
                  "member_no", "has_account", "created_at"]
        read_only_fields = fields

    def get_has_account(self, obj) -> bool:
        return User.objects.filter(
            identity_q(email=obj.email, phone=obj.phone),
            is_active=True).exists()


class JoinRequestViewSet(TenantScopedViewMixin,
                         mixins.ListModelMixin,
                         mixins.RetrieveModelMixin,
                         viewsets.GenericViewSet):
    """Membership requests waiting on this society's officers."""

    serializer_class = JoinRequestSerializer
    # Officers only, reads included: a pending request carries a stranger's name,
    # email and phone, which is not the whole membership's business.
    permission_classes = [IsPrivilegedOfficer]

    def get_queryset(self):
        qs = JoinRequest.objects.select_related("decided_by", "membership")
        state = self.request.query_params.get("status")
        return qs.filter(status=state) if state else qs

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        # The member number is assigned automatically; any sent is ignored.
        join_request = self.get_object()
        # Their chosen password is used only if approving creates the account.
        chose_password = bool(join_request.password_hash) and not (
            User.objects.filter(identity_q(email=join_request.email,
                                           phone=join_request.phone)).exists())
        try:
            membership = approve_join_request(join_request, actor=request.user)
        except SignupError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc

        user = membership.user
        if chose_password:
            told = "They can sign in with the password they chose."
        elif user.has_usable_password():
            told = ("They can switch to this society from their account."
                    if not user.email else
                    "They've been emailed to say they can switch to this "
                    "society from their account.")
        elif user.email:
            told = "They've been emailed a link to set their password."
        else:
            told = ("They have no email, so give them an access code from "
                    "their member record to set their password.")
        return Response({
            "detail": f"{membership.user.full_name} has been admitted. {told}",
            "membership": membership.id,
            # No email and no password: the console offers an access code.
            "needs_access_code": not (user.email
                                      or user.has_usable_password()),
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


# ── The signed-in person: joining another society ───────────────────────────
class MyJoinRequestSerializer(serializers.ModelSerializer):
    cooperative_id = serializers.IntegerField(source="cooperative.id")
    cooperative_name = serializers.CharField(source="cooperative.name")
    cooperative_logo = serializers.ImageField(source="cooperative.logo",
                                              read_only=True)

    class Meta:
        model = JoinRequest
        fields = ["id", "cooperative_id", "cooperative_name",
                  "cooperative_logo", "status", "decision_note", "message",
                  "created_at", "decided_at"]
        read_only_fields = fields


class MyJoinRequestCreateSerializer(serializers.Serializer):
    """The society by id (picked from the search) or by invite code/link."""

    cooperative = serializers.IntegerField(required=False)
    code = serializers.CharField(max_length=300, required=False,
                                 allow_blank=True)
    message = serializers.CharField(max_length=500, required=False,
                                    allow_blank=True, default="")

    def validate(self, attrs):
        societies = Cooperative.objects.filter(
            status=Cooperative.Status.ACTIVE)
        code = (attrs.get("code") or "").strip().rstrip("/")
        if attrs.get("cooperative"):
            society = societies.filter(pk=attrs["cooperative"]).first()
        elif code:
            # A pasted invite link works as well as the bare code.
            code = code.rsplit("/join/", 1)[-1].split("?")[0].strip("/")
            society = societies.filter(join_code__iexact=code,
                                       join_link_enabled=True).first()
        else:
            raise serializers.ValidationError(
                {"cooperative": ["Choose a cooperative to join."]})
        if society is None:
            raise serializers.ValidationError(
                {"cooperative": ["We couldn't find that cooperative, or it "
                                 "isn't accepting requests."]})
        attrs["society"] = society
        return attrs


def _requests_of(user):
    """Join requests filed with this person's email or phone."""
    return identity_q(email=user.email, phone=user.phone)


class MyJoinRequestsView(APIView):
    """`/me/join-requests/` — ask another society to admit you, with the
    account you already have.

    GET lists your requests (waiting, and recently declined); POST files one
    from your own name, email and phone, so there is nothing to retype and no
    second account. Officers decide it in the same queue as any other request;
    approving adds a membership to this account, and the society appears in
    your switcher.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def get_throttles(self):
        # Only filing a request costs officers' time and sends mail.
        return super().get_throttles() if self.request.method == "POST" else []

    def _mine(self, request):
        return (JoinRequest.all_objects
                .filter(_requests_of(request.user))
                .exclude(status=JoinRequest.Status.APPROVED)
                .select_related("cooperative").order_by("-created_at"))

    def get(self, request):
        return Response(MyJoinRequestSerializer(
            self._mine(request)[:20], many=True,
            context={"request": request}).data)

    def post(self, request):
        serializer = MyJoinRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        try:
            join_request = request_to_join(
                cooperative=data["society"], full_name=user.full_name,
                email=user.email or "", phone=user.phone or "",
                message=data["message"])
        except SignupError as exc:
            raise serializers.ValidationError({"detail": [str(exc)]}) from exc
        return Response(MyJoinRequestSerializer(
            join_request, context={"request": request}).data,
            status=status.HTTP_201_CREATED)


class MyJoinRequestDetailView(APIView):
    """`DELETE /me/join-requests/<id>/` — withdraw a request still waiting."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        join_request = JoinRequest.all_objects.filter(
            _requests_of(request.user), pk=pk,
            status=JoinRequest.Status.PENDING).first()
        if join_request is None:
            raise Http404
        join_request.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
