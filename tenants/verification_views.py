"""
Endpoints for a cooperative's KYC.

Console (the active cooperative, from the tenant header):

* ``GET  /verification/``          — the checklist; any office-holder may look.
* ``POST /verification/submit/``   — a privileged officer sends it for review.
* ``/cooperative-documents/``      — the society's own papers (privileged write).

Platform:

* ``GET  /platform-verifications/?status=submitted`` — the review queue.
* ``GET  /platform-verifications/<coop id>/``         — one society, with every
  document a reviewer needs, officer IDs included.
* ``POST .../approve/`` and ``.../request-changes/``.
"""
from __future__ import annotations

from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from core.context import get_current_cooperative
from core.permissions import (
    IsPrivilegedOfficer, IsPrivilegedOfficerOrOfficeHolderReadOnly,
)
from core.views import TenantScopedViewMixin
from tenants import verification as kyc
from tenants.models import CooperativeDocument, CooperativeVerification
from tenants.views import IsPlatformAdmin


class CooperativeDocumentSerializer(serializers.ModelSerializer):
    doc_type_display = serializers.CharField(source="get_doc_type_display",
                                             read_only=True)

    class Meta:
        model = CooperativeDocument
        fields = ["id", "doc_type", "doc_type_display", "label", "file",
                  "created_at"]

    def to_representation(self, instance):
        # KYC material: a signed, expiring URL, as for member documents.
        from core.media import sign_private_urls

        return sign_private_urls(
            super().to_representation(instance), "file",
            request=self.context.get("request"),
        )


def _decision_error(exc):
    return Response({"detail": str(exc)}, status=400)


# ── Console ─────────────────────────────────────────────────────────────────
class VerificationViewSet(TenantScopedViewMixin, viewsets.ViewSet):
    permission_classes = [IsPrivilegedOfficerOrOfficeHolderReadOnly]

    def list(self, request):
        return Response(kyc.checklist(get_current_cooperative()))

    @action(detail=False, methods=["post"],
            permission_classes=[IsPrivilegedOfficer])
    def submit(self, request):
        cooperative = get_current_cooperative()
        try:
            kyc.submit(cooperative, actor=request.user)
        except kyc.VerificationError as exc:
            return _decision_error(exc)
        return Response(kyc.checklist(cooperative))


class CooperativeDocumentViewSet(TenantScopedViewMixin,
                                 mixins.ListModelMixin,
                                 mixins.CreateModelMixin,
                                 mixins.DestroyModelMixin,
                                 viewsets.GenericViewSet):
    serializer_class = CooperativeDocumentSerializer
    permission_classes = [IsPrivilegedOfficerOrOfficeHolderReadOnly]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return CooperativeDocument.objects.all()

    def perform_create(self, serializer):
        serializer.save(uploaded_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        # Once a submission is with the reviewer, the papers they are reading
        # must not change under them.
        status = kyc.verification_for(get_current_cooperative()).status
        if status == CooperativeVerification.Status.SUBMITTED:
            return Response(
                {"detail": "This is with us for review, so documents cannot "
                           "be removed until we have decided."},
                status=400)
        return super().destroy(request, *args, **kwargs)


# ── Platform ────────────────────────────────────────────────────────────────
class PlatformVerificationSerializer(serializers.ModelSerializer):
    cooperative_id = serializers.IntegerField(source="cooperative.id")
    cooperative_name = serializers.CharField(source="cooperative.name")
    status_display = serializers.CharField(source="get_status_display")
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", default=None)
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.full_name", default=None)

    class Meta:
        model = CooperativeVerification
        fields = ["id", "cooperative_id", "cooperative_name", "status",
                  "status_display", "submitted_at", "submitted_by_name",
                  "reviewed_at", "reviewed_by_name", "reviewer_note",
                  "created_at"]


class PlatformVerificationViewSet(viewsets.ReadOnlyModelViewSet):
    """Keyed by cooperative id, which is what every other platform page has."""

    serializer_class = PlatformVerificationSerializer
    permission_classes = [IsPlatformAdmin]
    lookup_field = "cooperative_id"

    def get_queryset(self):
        qs = CooperativeVerification.objects.select_related(
            "cooperative", "submitted_by", "reviewed_by")
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    def retrieve(self, request, *args, **kwargs):
        from accounts.models import MemberDocument
        from accounts.serializers import MemberDocumentSerializer

        verification = self.get_object()
        coop = verification.cooperative
        checklist = kyc.checklist(coop)
        holder_ids = [p["membership_id"] for o in checklist["officers"]
                      for p in o["holders"]]
        context = {"request": request}
        officer_docs = (MemberDocument.all_objects
                        .filter(cooperative=coop, membership_id__in=holder_ids)
                        .select_related("membership"))
        return Response({
            **self.get_serializer(verification).data,
            "checklist": checklist,
            "documents": CooperativeDocumentSerializer(
                CooperativeDocument.all_objects.filter(cooperative=coop),
                many=True, context=context).data,
            "officer_documents": MemberDocumentSerializer(
                officer_docs, many=True, context=context).data,
            "registration_no": coop.registration_no,
        })

    @action(detail=True, methods=["post"])
    def approve(self, request, cooperative_id=None):
        verification = self.get_object()
        try:
            kyc.approve(verification.cooperative, actor=request.user)
        except kyc.VerificationError as exc:
            return _decision_error(exc)
        verification.refresh_from_db()
        return Response(self.get_serializer(verification).data)

    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, cooperative_id=None):
        verification = self.get_object()
        try:
            kyc.request_changes(verification.cooperative, actor=request.user,
                                note=request.data.get("note", ""))
        except kyc.VerificationError as exc:
            return _decision_error(exc)
        verification.refresh_from_db()
        return Response(self.get_serializer(verification).data)
