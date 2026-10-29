"""
Admin for announcements, per-member channel preferences and notifications.

``reach``/``sent_at`` on an announcement are written by the fan-out service, so
they are read-only here — editing them would misreport delivery.
"""
from __future__ import annotations

from django.contrib import admin

from communications.models import (Announcement, ChannelPreference,
                                   MemberEmail, MemberEmailRecipient,
                                   Notification)
from core.admin import TenantScopedModelAdmin, TenantScopedTabularInline


@admin.register(ChannelPreference)
class ChannelPreferenceAdmin(TenantScopedModelAdmin):
    list_display = ("membership", "cooperative", "in_app", "email", "sms",
                    "whatsapp", "opted_out")
    list_filter = ("opted_out", "in_app", "email", "sms", "whatsapp",
                   "cooperative")
    search_fields = ("membership__member_no", "membership__user__full_name",
                     "membership__user__email")
    autocomplete_fields = ("cooperative", "membership")
    list_select_related = ("membership", "cooperative")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Announcement)
class AnnouncementAdmin(TenantScopedModelAdmin):
    list_display = ("title", "cooperative", "audience", "audience_role",
                    "status", "reach", "sent_at")
    list_filter = ("status", "audience", "cooperative", "sent_at")
    search_fields = ("title", "body")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "audience_role", "created_by")
    list_select_related = ("audience_role", "cooperative")
    # Delivery facts belong to the fan-out service.
    readonly_fields = ("reach", "sent_at", "created_at", "updated_at")


@admin.register(Notification)
class NotificationAdmin(TenantScopedModelAdmin):
    list_display = ("title", "kind", "membership", "read", "created_at",
                    "cooperative")
    list_filter = ("kind", "read", "cooperative", "created_at")
    search_fields = ("title", "body", "membership__member_no",
                     "membership__user__full_name")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "membership", "announcement")
    list_select_related = ("membership", "announcement", "cooperative")
    readonly_fields = ("created_at", "updated_at")


class MemberEmailRecipientInline(TenantScopedTabularInline):
    model = MemberEmailRecipient
    extra = 0
    can_delete = False
    fields = ("address", "name", "member_no", "status", "sent_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(MemberEmailRecipient)
class MemberEmailRecipientAdmin(TenantScopedModelAdmin):
    list_display = ("address", "email", "cooperative", "status", "sent_at")
    list_filter = ("status", "cooperative")
    search_fields = ("address", "member_no", "email__subject")
    list_select_related = ("email", "cooperative")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MemberEmail)
class MemberEmailAdmin(TenantScopedModelAdmin):
    """Read-only record: officers write and send these from the console's
    Email page, which keeps the per-recipient log honest."""

    list_display = ("subject", "cooperative", "purpose", "audience", "status",
                    "sent_by", "sent_at")
    list_filter = ("status", "purpose", "audience", "cooperative")
    search_fields = ("subject",)
    list_select_related = ("cooperative", "sent_by")
    inlines = [MemberEmailRecipientInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
