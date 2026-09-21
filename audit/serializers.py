from __future__ import annotations

from rest_framework import serializers

from audit.models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "actor_name", "action", "entity_type", "entity_id",
                  "entity_repr", "before", "after", "created_at"]

    def get_actor_name(self, obj):
        return obj.actor_label or (obj.actor and obj.actor.full_name) or "System"
