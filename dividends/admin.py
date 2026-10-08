"""
Admin for dividend declarations and their per-member allocations.

Allocations are computed pro-rata to share capital and posted to the ledger by
``dividends.services``; once a declaration is posted its journal and allocations
are history. The journal link and posting timestamp are therefore read-only.
"""
from __future__ import annotations

from django.contrib import admin

from core.admin import (TenantScopedModelAdmin, TenantScopedTabularInline,
                        TwoFactorRequiredMixin)
from dividends.models import DividendAllocation, DividendDeclaration


class DividendAllocationInline(TenantScopedTabularInline):
    """Read-only: an allocation edited here would no longer match the journal
    that paid it, and a member's statement would disagree with the record."""

    model = DividendAllocation
    extra = 0
    fields = ("membership", "share_capital", "amount")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DividendDeclaration)
class DividendDeclarationAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("period_label", "cooperative", "total_amount", "status",
                    "posted_at", "created_by")
    list_filter = ("status", "cooperative", "posted_at")
    search_fields = ("period_label", "note")
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "created_by")
    list_select_related = ("cooperative", "created_by")
    # Declaring, posting and reversing are the services' job (through the
    # console and the approvals queue); every figure here is their record.
    readonly_fields = ("period_label", "method", "rate", "total_amount",
                       "status", "journal", "posted_at", "reversal_journal",
                       "reversed_at", "reversed_by", "reversal_reason",
                       "created_by", "created_at", "updated_at")
    inlines = (DividendAllocationInline,)

    def has_add_permission(self, request):
        return False


@admin.register(DividendAllocation)
class DividendAllocationAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("declaration", "membership", "share_capital", "amount",
                    "cooperative")
    list_filter = ("cooperative", "declaration")
    search_fields = ("membership__member_no", "membership__user__full_name",
                     "declaration__period_label")
    ordering = ("-amount",)
    autocomplete_fields = ("cooperative", "declaration", "membership")
    list_select_related = ("declaration", "membership", "cooperative")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
