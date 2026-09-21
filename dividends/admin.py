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
    model = DividendAllocation
    extra = 0
    autocomplete_fields = ("membership",)
    fields = ("membership", "share_capital", "amount")


@admin.register(DividendDeclaration)
class DividendDeclarationAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("period_label", "cooperative", "total_amount", "status",
                    "posted_at", "created_by")
    list_filter = ("status", "cooperative", "posted_at")
    search_fields = ("period_label", "note")
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "created_by")
    list_select_related = ("cooperative", "created_by")
    # Posting is the service's job; the journal is the ledger's record of it.
    readonly_fields = ("journal", "posted_at", "created_at", "updated_at")
    inlines = (DividendAllocationInline,)


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
