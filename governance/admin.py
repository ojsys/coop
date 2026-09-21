"""
Admin for meetings, attendance, resolutions and voting.

``Vote`` is append-only — ballots are immutable so results stay auditable — and
is therefore exposed read-only, both standalone and as an inline on the
resolution it belongs to.
"""
from __future__ import annotations

from django.contrib import admin

from core.admin import (AppendOnlyAdmin, TenantScopedModelAdmin,
                        TenantScopedTabularInline)
from governance.models import Attendance, Meeting, Resolution, Vote


class AttendanceInline(TenantScopedTabularInline):
    model = Attendance
    extra = 0
    autocomplete_fields = ("membership", "proxy_for")
    fields = ("membership", "present", "proxy_for")


@admin.register(Meeting)
class MeetingAdmin(TenantScopedModelAdmin):
    list_display = ("title", "cooperative", "scheduled_at", "status",
                    "location", "proxy_enabled")
    list_filter = ("status", "proxy_enabled", "cooperative", "scheduled_at")
    search_fields = ("title", "location", "agenda")
    date_hierarchy = "scheduled_at"
    ordering = ("-scheduled_at",)
    autocomplete_fields = ("cooperative",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (AttendanceInline,)

    fieldsets = (
        (None, {
            "fields": ("cooperative", "title", "location", "status"),
        }),
        ("Schedule", {
            "fields": ("scheduled_at", "voting_opens_at", "proxy_enabled"),
        }),
        ("Papers", {
            "fields": ("agenda", "minutes"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )


@admin.register(Attendance)
class AttendanceAdmin(TenantScopedModelAdmin):
    list_display = ("meeting", "membership", "present", "proxy_for",
                    "cooperative")
    list_filter = ("present", "cooperative")
    search_fields = ("meeting__title", "membership__member_no",
                     "membership__user__full_name")
    autocomplete_fields = ("cooperative", "meeting", "membership", "proxy_for")
    list_select_related = ("meeting", "membership", "proxy_for", "cooperative")


class VoteInline(TenantScopedTabularInline):
    model = Vote
    extra = 0
    can_delete = False
    fields = ("membership", "choice", "weight", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Resolution)
class ResolutionAdmin(TenantScopedModelAdmin):
    list_display = ("title", "cooperative", "meeting", "status", "voting_mode",
                    "outcome", "opens_at", "closes_at")
    list_filter = ("status", "outcome", "voting_mode", "cooperative")
    search_fields = ("title", "description")
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "meeting", "created_by")
    list_select_related = ("meeting", "cooperative")
    readonly_fields = ("created_at", "updated_at")
    inlines = (VoteInline,)


@admin.register(Vote)
class VoteAdmin(AppendOnlyAdmin):
    list_display = ("resolution", "membership", "choice", "weight",
                    "created_at", "cooperative")
    list_filter = ("choice", "cooperative", "created_at")
    search_fields = ("resolution__title", "membership__member_no",
                     "membership__user__full_name")
    ordering = ("-created_at", "-id")
    list_select_related = ("resolution", "membership", "cooperative")
