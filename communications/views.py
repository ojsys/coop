from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Membership
from communications.models import Announcement, Notification
from communications.serializers import (
    AnnouncementSerializer, NotificationSerializer,
)
from communications.services import broadcast
from core.views import TenantScopedViewMixin


class AnnouncementViewSet(TenantScopedViewMixin, viewsets.ModelViewSet):
    serializer_class = AnnouncementSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Announcement.objects.all()

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Fan the announcement out across its selected channels."""
        announcement = self.get_object()
        try:
            reach = broadcast(announcement)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        announcement.refresh_from_db()
        data = AnnouncementSerializer(announcement).data
        data["reach"] = reach
        return Response(data)


class NotificationViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """The requesting member's in-app notifications."""

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        membership = Membership.objects.filter(user=self.request.user).first()
        if membership is None:
            return Notification.objects.none()
        return Notification.objects.filter(membership=membership)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_object()
        notification.read = True
        notification.save(update_fields=["read", "updated_at"])
        return Response(NotificationSerializer(notification).data)
