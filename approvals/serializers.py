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
    # Decided by the person who submitted it: only possible for a sole
    # Secretary, and shown so nobody mistakes it for a checked request later.
    self_decided = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalRequest
        fields = ["id", "action", "action_display", "object_id", "summary",
                  "status", "status_display", "requested_by",
                  "requested_by_name", "decided_by_name", "self_decided",
                  "decided_at", "note", "created_at"]
        read_only_fields = ["status", "summary", "requested_by", "decided_at"]

    def get_self_decided(self, obj) -> bool:
        return bool(obj.decided_by_id
                    and obj.decided_by_id == obj.requested_by_id)
