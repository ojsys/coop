from __future__ import annotations

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Membership
from core.views import TenantScopedViewMixin
from governance.models import Attendance, Meeting, Resolution
from governance.serializers import (
    AttendanceSerializer, MeetingSerializer, ResolutionSerializer,
    VoteSerializer,
)
from governance.services import (
    GovernanceError, cast_vote, close_resolution, open_resolution, tally,
)


class MeetingViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = MeetingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Meeting.objects.all()


class AttendanceViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    """Record who attended a meeting (present / by proxy)."""

    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        qs = Attendance.objects.select_related("meeting", "membership__user")
        meeting = self.request.query_params.get("meeting")
        if meeting:
            qs = qs.filter(meeting_id=meeting)
        return qs


class ResolutionViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = ResolutionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Resolution.objects.all()

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def open(self, request, pk=None):
        resolution = self.get_object()
        try:
            open_resolution(
                resolution, opens_at=request.data.get("opens_at"),
                closes_at=request.data.get("closes_at"), actor=request.user,
            )
        except GovernanceError as exc:
            raise ValidationError(str(exc))
        return Response(ResolutionSerializer(resolution).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        resolution = self.get_object()
        try:
            close_resolution(resolution, actor=request.user)
        except GovernanceError as exc:
            raise ValidationError(str(exc))
        return Response(ResolutionSerializer(resolution).data)

    @action(detail=True, methods=["get"])
    def results(self, request, pk=None):
        return Response(tally(self.get_object()))

    @action(detail=True, methods=["post"])
    def vote(self, request, pk=None):
        """Cast the requesting member's ballot on this resolution."""
        resolution = self.get_object()
        membership_id = request.data.get("membership")
        if membership_id:
            membership = Membership.objects.filter(pk=membership_id).first()
        else:
            membership = Membership.objects.filter(user=request.user).first()
        if membership is None:
            raise ValidationError("No membership found to vote with.")
        try:
            vote = cast_vote(resolution, membership, request.data.get("choice"))
        except GovernanceError as exc:
            raise ValidationError(str(exc))
        return Response(VoteSerializer(vote).data, status=201)
