from __future__ import annotations

from rest_framework import serializers

from approvals.models import ApprovalRequest


class ApprovalRequestSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display",
                                           read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.full_name", read_only=True, default=None)
    decided_by_name = serializers.CharField(
        source="decided_by.full_name", read_only=True, default=None)

    class Meta:
        model = ApprovalRequest
        fields = ["id", "action", "action_display", "object_id", "summary",
                  "status", "status_display", "requested_by",
                  "requested_by_name", "decided_by_name", "decided_at", "note",
                  "created_at"]
        read_only_fields = ["status", "summary", "requested_by", "decided_at"]
