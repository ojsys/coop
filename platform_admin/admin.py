"""
Admin for platform-level (Startup Ripple) operations.

Nothing here is tenant-scoped — these tables describe the business of running
the platform and aggregate across every cooperative — so they use plain
``ModelAdmin`` with the standard manager.
"""
from __future__ import annotations

from django.contrib import admin, messages

from platform_admin.models import (Domain, Incident, Invoice,
                                   NotificationTemplate, OnboardingItem, Plan,
                                   PlatformProfile, PlatformTeamMember,
                                   ProviderCheck, ProviderStatus,
                                   Subscription, SupportTicket)


# ── Billing ────────────────────────────────────────────────────────────────
@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "tier", "price_monthly", "currency", "min_members",
                    "max_members", "active")
    list_filter = ("tier", "active", "currency")
    search_fields = ("name", "description")
    ordering = ("price_monthly", "name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("cooperative", "plan", "status", "started_at",
                    "current_period_start", "current_period_end")
    list_filter = ("status", "plan")
    search_fields = ("cooperative__name", "plan__name")
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "plan")
    list_select_related = ("cooperative", "plan")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "cooperative", "period_label", "amount",
                    "currency", "status", "issued_at", "due_at", "paid_at")
    list_filter = ("status", "currency", "issued_at", "due_at")
    search_fields = ("number", "period_label", "cooperative__name")
    date_hierarchy = "issued_at"
    ordering = ("-issued_at", "-created_at")
    autocomplete_fields = ("cooperative", "subscription")
    list_select_related = ("cooperative", "subscription")
    readonly_fields = ("created_at", "updated_at")


# ── Onboarding pipeline ─────────────────────────────────────────────────────
@admin.register(OnboardingItem)
class OnboardingItemAdmin(admin.ModelAdmin):
    list_display = ("display_name", "stage", "owner", "target_go_live",
                    "cooperative")
    list_filter = ("stage", "owner", "target_go_live")
    search_fields = ("prospect_name", "cooperative__name", "notes")
    ordering = ("stage", "-created_at")
    autocomplete_fields = ("cooperative", "owner")
    list_select_related = ("cooperative", "owner")
    readonly_fields = ("created_at", "updated_at")
    actions = ("advance_stage",)

    @admin.display(description="Cooperative / prospect")
    def display_name(self, obj):
        return obj.display_name

    @admin.action(description="Advance to the next pipeline stage")
    def advance_stage(self, request, queryset):
        """Reaching go-live activates the linked cooperative (and is audited)."""
        for item in queryset:
            item.advance(actor=request.user)
        self.message_user(
            request, f"Advanced {queryset.count()} onboarding item(s).",
            messages.SUCCESS,
        )


# ── White-label domains ─────────────────────────────────────────────────────
@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "cooperative", "is_primary", "dns_status",
                    "ssl_status", "verified_at")
    list_filter = ("dns_status", "ssl_status", "is_primary")
    search_fields = ("domain", "cooperative__name")
    ordering = ("domain",)
    autocomplete_fields = ("cooperative",)
    list_select_related = ("cooperative",)
    readonly_fields = ("created_at", "updated_at")


# ── Provider / system health ────────────────────────────────────────────────
class ProviderCheckInline(admin.TabularInline):
    model = ProviderCheck
    extra = 0
    fields = ("status", "latency_ms", "checked_at")
    readonly_fields = fields
    ordering = ("-checked_at",)

    def has_add_permission(self, request, obj=None):
        # Samples are written by the health poller.
        return False


@admin.register(ProviderStatus)
class ProviderStatusAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "status", "latency_ms", "checked_at")
    list_filter = ("kind", "status")
    search_fields = ("name",)
    ordering = ("kind", "name")
    readonly_fields = ("created_at", "updated_at")
    inlines = (ProviderCheckInline,)


@admin.register(ProviderCheck)
class ProviderCheckAdmin(admin.ModelAdmin):
    list_display = ("provider", "status", "latency_ms", "checked_at")
    list_filter = ("status", "provider")
    search_fields = ("provider__name",)
    date_hierarchy = "checked_at"
    ordering = ("-checked_at",)
    autocomplete_fields = ("provider",)
    list_select_related = ("provider",)


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "status", "started_at", "resolved_at")
    list_filter = ("severity", "status", "started_at")
    search_fields = ("title", "note")
    date_hierarchy = "started_at"
    ordering = ("-started_at",)
    readonly_fields = ("created_at", "updated_at")


# ── Support tickets ─────────────────────────────────────────────────────────
@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "cooperative", "priority", "status",
                    "created_by", "created_at", "closed_at")
    list_filter = ("status", "priority", "created_at")
    search_fields = ("subject", "body", "cooperative__name")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "created_by")
    list_select_related = ("cooperative", "created_by")
    readonly_fields = ("created_at", "updated_at")


# ── Platform profile & notification templates ───────────────────────────────
@admin.register(PlatformProfile)
class PlatformProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "support_email", "default_currency",
                    "default_timezone", "brand_color")
    readonly_fields = ("created_at", "updated_at")

    def has_add_permission(self, request):
        # Singleton — one platform profile, loaded via PlatformProfile.load().
        return not PlatformProfile.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "channel", "subject", "active")
    list_filter = ("channel", "active")
    search_fields = ("key", "name", "subject", "body")
    ordering = ("name",)
    prepopulated_fields = {"key": ("name",)}
    readonly_fields = ("created_at", "updated_at")


# ── Platform team ───────────────────────────────────────────────────────────
@admin.register(PlatformTeamMember)
class PlatformTeamMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "active")
    list_filter = ("role", "active")
    search_fields = ("user__full_name", "user__email")
    ordering = ("role", "user__full_name")
    autocomplete_fields = ("user",)
    list_select_related = ("user",)
    readonly_fields = ("created_at", "updated_at")
