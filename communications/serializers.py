from __future__ import annotations

from rest_framework import serializers

from communications.models import (
    Announcement, ChannelPreference, Notification,
)


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = ["id", "title", "body", "audience", "audience_role",
                  "channels", "status", "reach", "sent_at", "created_at"]
        read_only_fields = ["status", "reach", "sent_at"]


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "body", "read", "created_at"]


class ChannelPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChannelPreference
        fields = ["id", "membership", "in_app", "email", "sms", "whatsapp",
                  "opted_out"]
