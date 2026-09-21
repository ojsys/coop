"""
Admin for announcements, per-member channel preferences and notifications.

``reach``/``sent_at`` on an announcement are written by the fan-out service, so
they are read-only here — editing them would misreport delivery.
"""
from __future__ import annotations

from django.contrib import admin

from communications.models import (Announcement, ChannelPreference,
                                   Notification)
from core.admin import TenantScopedModelAdmin


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
