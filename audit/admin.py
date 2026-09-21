"""
Admin for the immutable audit log.

``AuditLog`` is append-only and tamper-evident — it is a compliance asset, so
the admin never lets anyone add, edit or delete a row. It is a viewer only.
"""
from __future__ import annotations

from django.contrib import admin

from audit.models import AuditLog
from core.admin import AppendOnlyAdmin


@admin.register(AuditLog)
class AuditLogAdmin(AppendOnlyAdmin):
    list_display = ("created_at", "action", "who", "entity_type", "entity_repr",
                    "cooperative")
    list_filter = ("action", "entity_type", "cooperative", "created_at")
    search_fields = ("action", "entity_type", "entity_id", "entity_repr",
                     "actor_label", "actor__full_name", "actor__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    list_select_related = ("actor", "cooperative")

    @admin.display(description="Actor")
    def who(self, obj):
        return obj.actor_label or (obj.actor and obj.actor.full_name) or "—"
