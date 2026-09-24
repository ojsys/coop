"""
Admin for platform-level (Startup Ripple) operations.

Nothing here is tenant-scoped — these tables describe the business of running
the platform and aggregate across every cooperative — so they use plain
``ModelAdmin`` with the standard manager.
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html

from platform_admin.models import (Domain, Incident, Invoice,
                                   NotificationTemplate, OnboardingItem, Plan,
                                   PlatformProfile, PlatformTeamMember,
                                   ProviderCheck, ProviderStatus, SiteContent,
                                   SiteFeature, SiteGalleryImage, SiteStep,
                                   SiteTrustBadge, Subscription, SupportTicket)


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
    readonly_fields = ("logo_preview", "favicon_preview", "created_at",
                       "updated_at")

    fieldsets = (
        (None, {"fields": ("name", "support_email", "support_phone")}),
        ("Branding", {
            "fields": ("brand_color", "logo", "logo_preview",
                       "favicon", "favicon_preview"),
        }),
        ("Defaults applied to new cooperatives", {
            "fields": ("default_currency", "default_timezone"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.display(description="Logo preview")
    def logo_preview(self, obj):
        if not obj.logo:
            return "—"
        return format_html(
            '<img src="{}" alt="" style="max-height:72px;border-radius:6px;'
            'background:#f4f1ea;padding:4px">', obj.logo.url,
        )

    @admin.display(description="Favicon preview")
    def favicon_preview(self, obj):
        if not obj.favicon:
            return "—"
        return format_html(
            '<img src="{}" alt="" style="height:32px;width:32px;'
            'border-radius:6px">', obj.favicon.url,
        )

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


# ── Public site content (the CMS) ───────────────────────────────────────────
# These screens are used by non-technical staff, so they are organised by
# where things appear on the page rather than by model structure, and every
# field carries help text written for someone who has never seen the code.
@admin.register(SiteContent)
class SiteContentAdmin(admin.ModelAdmin):
    list_display = ("__str__", "hero_title", "updated_at")
    readonly_fields = ("hero_image_preview", "created_at", "updated_at")

    fieldsets = (
        ("Top of the page (hero)", {
            "description": "The first thing a visitor reads.",
            "fields": ("hero_eyebrow", "hero_title", "hero_subtitle",
                       "hero_primary_cta", "hero_secondary_cta",
                       "hero_image", "hero_image_preview", "hero_image_alt"),
        }),
        ("The green band", {
            "fields": ("principle_eyebrow", "principle_title",
                       "principle_body"),
        }),
        ("Section headings", {
            "description": "Headings only — the cards, steps and photos "
                           "themselves are edited on their own screens.",
            "fields": ("features_title", "features_intro", "steps_title",
                       "gallery_title", "gallery_intro"),
        }),
        ("Pricing", {
            "description": "Prices come from the Plans table and are never "
                           "typed here, so published pricing cannot drift "
                           "from what societies are actually billed.",
            "fields": ("pricing_title", "pricing_intro", "pricing_fallback"),
        }),
        ("Footer", {"fields": ("footer_tagline", "footer_disclaimer")}),
        ("Search engines & link previews", {
            "classes": ("collapse",),
            "fields": ("meta_title", "meta_description"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.display(description="Current hero image")
    def hero_image_preview(self, obj):
        if not obj.hero_image:
            return "No image — the page shows a plain panel instead."
        return format_html(
            '<img src="{}" alt="" style="max-height:160px;max-width:100%;'
            'border-radius:8px">', obj.hero_image.url,
        )

    def has_add_permission(self, request):
        # Singleton — one public site, loaded via SiteContent.load().
        return not SiteContent.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteFeature)
class SiteFeatureAdmin(admin.ModelAdmin):
    list_display = ("title", "icon", "order", "visible")
    list_editable = ("order", "visible")
    list_filter = ("visible",)
    search_fields = ("title", "body")
    ordering = ("order", "id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SiteStep)
class SiteStepAdmin(admin.ModelAdmin):
    list_display = ("number", "title", "order", "visible")
    list_editable = ("order", "visible")
    list_filter = ("visible",)
    search_fields = ("title", "body")
    ordering = ("order", "id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SiteTrustBadge)
class SiteTrustBadgeAdmin(admin.ModelAdmin):
    list_display = ("text", "icon", "order", "visible")
    list_editable = ("order", "visible")
    list_filter = ("visible",)
    search_fields = ("text",)
    ordering = ("order", "id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SiteGalleryImage)
class SiteGalleryImageAdmin(admin.ModelAdmin):
    list_display = ("thumbnail", "alt_text", "caption", "order", "visible")
    list_display_links = ("thumbnail", "alt_text")
    list_editable = ("order", "visible")
    list_filter = ("visible",)
    search_fields = ("alt_text", "caption")
    ordering = ("order", "id")
    readonly_fields = ("preview", "created_at", "updated_at")

    fieldsets = (
        (None, {
            "description": "Upload photographs you hold the rights to. Free "
                           "options with clear licences include Pexels, "
                           "Unsplash and Nappy — check each licence before "
                           "publishing.",
            "fields": ("image", "preview", "alt_text", "caption",
                       "order", "visible"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.display(description="")
    def thumbnail(self, obj):
        if not obj.image:
            return "—"
        return format_html(
            '<img src="{}" alt="" style="height:44px;width:70px;'
            'object-fit:cover;border-radius:4px">', obj.image.url,
        )

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.image:
            return "—"
        return format_html(
            '<img src="{}" alt="" style="max-height:220px;max-width:100%;'
            'border-radius:8px">', obj.image.url,
        )


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
