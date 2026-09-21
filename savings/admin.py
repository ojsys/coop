"""
Admin for savings products and per-member goals.

``saved_amount`` is derived (from confirmed contributions of the product's
contribution type, or the member's ledger balance) and never stored, so it is
shown as a computed column rather than an editable field.
"""
from __future__ import annotations

from django.contrib import admin

from core.admin import TenantScopedModelAdmin
from savings.models import SavingsGoal, SavingsProduct


@admin.register(SavingsProduct)
class SavingsProductAdmin(TenantScopedModelAdmin):
    list_display = ("name", "cooperative", "interest_rate", "contribution_type",
                    "active")
    list_filter = ("active", "cooperative")
    search_fields = ("name", "description")
    ordering = ("cooperative", "name")
    autocomplete_fields = ("cooperative", "contribution_type")
    list_select_related = ("contribution_type", "cooperative")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SavingsGoal)
class SavingsGoalAdmin(TenantScopedModelAdmin):
    # ``saved_amount`` is deliberately NOT a changelist column: its value
    # branches on the product's contribution type, so it cannot be annotated in
    # one aggregate and would cost a query per row. It shows on the detail page.
    list_display = ("name", "membership", "product", "target_amount",
                    "target_date", "cooperative")
    list_filter = ("cooperative", "product", "target_date")
    search_fields = ("name", "membership__member_no",
                     "membership__user__full_name")
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "membership", "product")
    list_select_related = ("membership", "product", "cooperative")
    readonly_fields = ("saved_amount", "created_at", "updated_at")

    @admin.display(description="Saved")
    def saved_amount(self, obj):
        """Derived from the ledger / confirmed contributions."""
        return "—" if obj.pk is None else obj.saved_amount
