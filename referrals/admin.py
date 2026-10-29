"""Admin for refer-and-earn. Money facts are read-only here: commissions are
written by invoice settlement and payouts decided on the platform's Referrals
page, which keep the audit trail."""
from __future__ import annotations

from django.contrib import admin

from referrals.models import (Commission, Referral, ReferralPayout,
                              ReferralProgram, Referrer)


@admin.register(ReferralProgram)
class ReferralProgramAdmin(admin.ModelAdmin):
    list_display = ("__str__", "active", "minimum_payout", "updated_at")


@admin.register(Referrer)
class ReferrerAdmin(admin.ModelAdmin):
    list_display = ("user", "code", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("code", "user__email", "user__full_name")
    readonly_fields = ("code", "created_at", "updated_at")
    autocomplete_fields = ("user",)


@admin.register(Referral)
class ReferralAdmin(admin.ModelAdmin):
    list_display = ("cooperative", "referrer", "commission_percent",
                    "commission_months", "first_paid_on", "created_at")
    search_fields = ("cooperative__name", "referrer__code")
    readonly_fields = ("first_paid_on", "created_at", "updated_at")
    autocomplete_fields = ("cooperative", "referrer")


class _ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Commission)
class CommissionAdmin(_ReadOnly):
    list_display = ("referral", "invoice", "amount", "status", "created_at")
    list_filter = ("status",)


@admin.register(ReferralPayout)
class ReferralPayoutAdmin(_ReadOnly):
    list_display = ("referrer", "amount", "status", "decided_at", "created_at")
    list_filter = ("status",)
