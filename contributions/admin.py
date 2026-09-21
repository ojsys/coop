"""
Admin for contribution types and recorded contributions.

A ``Contribution`` is the business-level record; the balanced journal it posted
is linked read-only so the accounting and business views stay reconciled. The
journal link is never editable here — repointing it by hand would silently break
that reconciliation. To undo a contribution, use the "void" action, which posts
a reversing journal through ``contributions.services.reverse_contribution``.
"""
from __future__ import annotations

from django.contrib import admin, messages

from contributions.models import Contribution, ContributionType
from core.admin import TenantScopedModelAdmin, TwoFactorRequiredMixin


@admin.register(ContributionType)
class ContributionTypeAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("name", "slug", "cooperative", "kind", "frequency",
                    "expected_amount", "gl_account", "active")
    list_filter = ("kind", "frequency", "active", "cooperative")
    search_fields = ("name", "slug")
    ordering = ("cooperative", "name")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("cooperative", "gl_account")
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("cooperative", "gl_account")


@admin.register(Contribution)
class ContributionAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("membership", "contribution_type", "amount", "channel",
                    "status", "occurred_at", "cooperative")
    list_filter = ("status", "channel", "cooperative", "contribution_type",
                   "occurred_at")
    search_fields = ("membership__member_no", "membership__user__full_name",
                     "psp_reference")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at", "-id")
    autocomplete_fields = ("cooperative", "membership", "contribution_type",
                           "recorded_by")
    list_select_related = ("membership", "contribution_type", "cooperative")
    # The posted journal is the ledger's business; never rewire it by hand.
    readonly_fields = ("journal", "created_at", "updated_at")
    actions = ("void_selected",)
    two_factor_actions = ("void_selected",)

    @admin.action(description="Void selected contributions (post a reversal)")
    def void_selected(self, request, queryset):
        """Reverse a contribution — e.g. a duplicate — via a reversing journal.

        The contribution is marked REVERSED and the reversal is recorded in the
        audit log; nothing is deleted.
        """
        from contributions.services import (ContributionError,
                                            reverse_contribution)

        voided = 0
        for contribution in queryset:
            try:
                reverse_contribution(
                    contribution, recorded_by=request.user,
                    memo="Voided from admin",
                )
            except ContributionError as exc:
                self.message_user(
                    request, f"Contribution #{contribution.pk}: {exc}",
                    messages.ERROR,
                )
            else:
                voided += 1
        if voided:
            self.message_user(
                request, f"Voided {voided} contribution(s).", messages.SUCCESS,
            )
