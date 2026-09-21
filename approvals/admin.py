"""
Admin for maker-checker approval requests.

Segregation of duties (checker != maker) is enforced in the approval service,
not in a form here, so the decision fields are shown read-only: a decision made
by editing this row would bypass that check and the action it gates would never
execute. Use the API/console to approve or reject.
"""
from __future__ import annotations

from django.contrib import admin

from approvals.models import ApprovalRequest
from core.admin import TenantScopedModelAdmin


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(TenantScopedModelAdmin):
    list_display = ("action", "object_id", "summary", "status", "requested_by",
                    "decided_by", "decided_at", "cooperative")
    list_filter = ("status", "action", "cooperative", "created_at")
    search_fields = ("summary", "note", "requested_by__full_name",
                     "requested_by__email")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("requested_by", "decided_by", "cooperative")
    autocomplete_fields = ("cooperative", "requested_by", "decided_by")
    # A decision must go through the service that enforces checker != maker.
    readonly_fields = ("status", "decided_by", "decided_at", "created_at",
                       "updated_at")

    def has_add_permission(self, request):
        # Requests are raised by the action being gated, never by hand.
        return False
