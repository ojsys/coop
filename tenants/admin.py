"""
Admin for the tenant root.

``Cooperative`` is deliberately *not* tenant-scoped — it is the thing everything
else is scoped to — so it uses a plain ``ModelAdmin`` with the standard manager.

Inlines show the tenant's platform-side attachments (billing, domains, PSP
subaccount). Members are deliberately *not* inlined: a cooperative can hold
thousands, so they belong on the Membership changelist with its filters.
"""
from __future__ import annotations

from django.contrib import admin, messages

from core.admin import TenantScopedTabularInline
from payments.models import ProviderAccount
from platform_admin.models import Domain, Subscription
from tenants.models import Cooperative


class SubscriptionInline(admin.StackedInline):
    model = Subscription
    extra = 0
    max_num = 1
    autocomplete_fields = ("plan",)
    fields = ("plan", "status", "started_at", "current_period_start",
              "current_period_end")


class DomainInline(admin.TabularInline):
    model = Domain
    extra = 0
    fields = ("domain", "is_primary", "dns_status", "ssl_status", "verified_at")


class ProviderAccountInline(TenantScopedTabularInline):
    model = ProviderAccount
    extra = 0
    fields = ("provider", "subaccount_code", "bank_name", "connected")


@admin.register(Cooperative)
class CooperativeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "tier", "state", "lga",
                    "base_currency", "member_cap", "created_at")
    list_filter = ("status", "tier", "base_currency", "state")
    search_fields = ("name", "slug", "registration_no", "state", "lga")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "created_at"
    ordering = ("name",)
    actions = ("seed_chart_of_accounts",)
    inlines = (SubscriptionInline, DomainInline, ProviderAccountInline)

    fieldsets = (
        ("Identity", {
            "fields": ("name", "slug", "registration_no", "coop_type"),
        }),
        ("Location", {
            "fields": ("state", "lga"),
        }),
        ("Plan & lifecycle", {
            "fields": ("tier", "status", "member_cap", "base_currency"),
        }),
        ("Branding", {
            "fields": ("brand_color",),
        }),
        ("Collection account", {
            "description": "The society's own bank account, shown to members "
                           "who repay by direct transfer.",
            "fields": ("bank_name", "bank_account_name", "bank_account_no"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    @admin.action(description="Seed the baseline chart of accounts")
    def seed_chart_of_accounts(self, request, queryset):
        """Provision the accounts a coop needs to post. Idempotent."""
        for cooperative in queryset:
            cooperative.seed_chart_of_accounts()
        self.message_user(
            request,
            f"Seeded the chart of accounts for {queryset.count()} cooperative(s).",
            messages.SUCCESS,
        )
