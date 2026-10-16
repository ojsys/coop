"""Endpoints for a person's own data: download it, or ask for deletion.

Three audiences: the signed-in person (``/me/...``), anyone with only an
email address (``/public/account-deletion/...`` — the route Google Play
requires, usable without the app), and a society's officers, who complete a
deletion once anything owed either way is settled.
"""
from __future__ import annotations

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.data_rights import (
    DeletionError, cancel_request, complete_deletion, deletion_blockers,
    export_user_data, pending_request, request_deletion, send_confirmation,
    user_from_token,
)
from accounts.models import AccountDeletionRequest, Membership
from core.context import get_current_cooperative
from core.permissions import IsPrivilegedOfficer
from core.public_views import PublicView
from core.views import TenantScopedViewMixin


def _request_payload(req):
    if req is None:
        return None
    return {"id": req.id, "status": req.status, "source": req.source,
            "reason": req.reason, "created_at": req.created_at}


# ── The signed-in person ────────────────────────────────────────────────────
class MyDataExportView(APIView):
    """`GET /me/data-export/` — everything held about you, as a zip."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from accounts.data_rights import memberships_of
        from audit.services import record_action

        data = export_user_data(request.user)
        for m in memberships_of(request.user):
            record_action(cooperative=m.cooperative, actor=request.user,
                          action="member.data_exported", entity=m)
        response = HttpResponse(data, content_type="application/zip")
        response["Content-Disposition"] = (
            f'attachment; filename="my-data-{timezone.localdate()}.zip"')
        return response


class MyDeletionRequestView(APIView):
    """`/me/deletion-request/` — GET the state, POST to ask, DELETE to cancel."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "request": _request_payload(pending_request(request.user)),
            "blockers": deletion_blockers(request.user),
        })

    def post(self, request):
        if request.user.is_platform_admin:
            return Response({"detail": "Platform staff accounts are closed "
                                       "by the platform team."}, status=400)
        req = request_deletion(
            request.user, reason=(request.data.get("reason") or "").strip())
        return Response({"request": _request_payload(req),
                         "blockers": deletion_blockers(request.user)},
                        status=201)

    def delete(self, request):
        req = pending_request(request.user)
        if req is None:
            return Response({"detail": "There is no pending request."},
                            status=404)
        cancel_request(req)
        return Response(status=204)


# ── Anyone, by email (no app, no password needed) ───────────────────────────
class PublicDeletionRequestView(PublicView):
    """`POST /public/account-deletion/` {email} — email a confirmation link.

    Answers the same whether or not the address has an account."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        email = serializers.EmailField().run_validation(
            request.data.get("email"))
        send_confirmation(email)
        return Response({"detail": "If that address has an account, we've "
                                   "emailed it a link to confirm the "
                                   "request."})


class PublicDeletionConfirmView(PublicView):
    """`POST /public/account-deletion/confirm/` {token} — file the request."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        user = user_from_token(request.data.get("token") or "")
        if user is None:
            return Response({"detail": "This link is invalid or has expired. "
                                       "Ask for a new one."}, status=400)
        request_deletion(user, source=AccountDeletionRequest.Source.WEB,
                         reason=(request.data.get("reason") or "").strip())
        return Response({"detail": "Your request has been sent to your "
                                   "cooperative. It will be completed within "
                                   "30 days, and we'll email you when it is.",
                         "blockers": deletion_blockers(user)})


# ── Officers ────────────────────────────────────────────────────────────────
class DeletionRequestSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="user.full_name")
    email = serializers.CharField(source="user.email")
    member_no = serializers.SerializerMethodField()
    membership_id = serializers.SerializerMethodField()
    blockers = serializers.SerializerMethodField()
    completed_by_name = serializers.CharField(
        source="completed_by.full_name", default=None)

    class Meta:
        model = AccountDeletionRequest
        fields = ["id", "full_name", "email", "member_no", "membership_id",
                  "status", "source", "reason", "blockers", "created_at",
                  "completed_at", "completed_by_name"]

    def _membership(self, obj):
        return Membership.all_objects.filter(
            user=obj.user, cooperative=get_current_cooperative()).first()

    def get_member_no(self, obj):
        m = self._membership(obj)
        return m.member_no if m else None

    def get_membership_id(self, obj):
        m = self._membership(obj)
        return m.id if m else None

    def get_blockers(self, obj):
        if obj.status != AccountDeletionRequest.Status.PENDING:
            return []
        return deletion_blockers(obj.user)


class DeletionRequestViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                             mixins.RetrieveModelMixin,
                             viewsets.GenericViewSet):
    """Deletion requests from this society's members."""

    serializer_class = DeletionRequestSerializer
    permission_classes = [IsPrivilegedOfficer]

    def get_queryset(self):
        qs = (AccountDeletionRequest.objects
              .filter(user__memberships__cooperative=get_current_cooperative())
              .select_related("user", "completed_by").distinct())
        status = self.request.query_params.get("status")
        return qs.filter(status=status) if status else qs

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        req = self.get_object()
        try:
            complete_deletion(req, actor=request.user)
        except DeletionError as exc:
            return Response({"detail": str(exc)}, status=400)
        req.refresh_from_db()
        return Response(self.get_serializer(req).data)
