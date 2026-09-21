"""
Admin for the double-entry ledger.

``Journal`` and ``LedgerEntry`` are append-only: the models raise on any update
or delete, so the admin exposes them as strictly read-only rather than letting a
save blow up in the form. Corrections are made by posting a reversal — available
here as the "post reversing entries" action, which calls the same
``ledger.services.reverse_journal`` the API uses.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib import admin, messages
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce

from core.admin import (AppendOnlyAdmin, TenantScopedModelAdmin,
                        TenantScopedTabularInline, TwoFactorActionsMixin,
                        TwoFactorRequiredMixin)
from ledger.models import Account, Journal, LedgerEntry

_MONEY = DecimalField(max_digits=16, decimal_places=2)
_ZERO = Value(Decimal("0.00"), output_field=_MONEY)


@admin.register(Account)
class AccountAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("code", "name", "kind", "cooperative", "system", "balance")
    list_filter = ("kind", "system", "cooperative")
    search_fields = ("code", "name")
    ordering = ("cooperative", "code")
    autocomplete_fields = ("cooperative",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("cooperative",)

    def get_queryset(self, request):
        # Sum the entries in one aggregate rather than per row — Account.balance
        # issues its own query, which would be an N+1 across the changelist.
        return super().get_queryset(request).annotate(
            _debit=Coalesce(Sum("entries__debit"), _ZERO, output_field=_MONEY),
            _credit=Coalesce(Sum("entries__credit"), _ZERO, output_field=_MONEY),
        )

    @admin.display(description="Balance")
    def balance(self, obj):
        """Derived from the ledger — never stored."""
        if obj.pk is None:
            return "—"
        debit = getattr(obj, "_debit", None)
        if debit is None:          # detail page: no annotation, fall back
            return obj.balance
        credit = obj._credit
        return (debit - credit) if obj.is_debit_normal else (credit - debit)

    def has_delete_permission(self, request, obj=None):
        # Accounts seeded at provisioning are structural; keep them.
        if obj is not None and obj.system:
            return False
        return super().has_delete_permission(request, obj)


class LedgerEntryInline(TenantScopedTabularInline):
    model = LedgerEntry
    extra = 0
    can_delete = False
    fields = ("account", "membership", "debit", "credit", "currency",
              "description")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Journal)
class JournalAdmin(TwoFactorActionsMixin, AppendOnlyAdmin):
    list_display = ("reference", "cooperative", "occurred_at", "posted_at",
                    "memo", "is_reversal", "is_reversed")
    list_filter = ("cooperative", "occurred_at")
    search_fields = ("reference", "memo")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at", "-id")
    list_select_related = ("cooperative",)
    inlines = (LedgerEntryInline,)
    actions = ("reverse_selected",)
    two_factor_actions = ("reverse_selected",)

    @admin.display(boolean=True, description="Reversal")
    def is_reversal(self, obj):
        return obj.is_reversal

    @admin.display(boolean=True, description="Reversed")
    def is_reversed(self, obj):
        return obj.is_reversed

    @admin.action(description="Post reversing entries for selected journals")
    def reverse_selected(self, request, queryset):
        """Correct a posting the only way the ledger allows — a mirror journal.

        The original is never touched; ``reverse_journal`` refuses to reverse a
        journal twice, or to reverse a reversal.
        """
        from ledger.services import LedgerError, reverse_journal

        posted = 0
        for journal in queryset:
            try:
                reversal = reverse_journal(
                    journal, created_by=request.user,
                    memo=f"Reversal of {journal.reference} (admin)",
                )
            except LedgerError as exc:
                self.message_user(
                    request, f"{journal.reference}: {exc}", messages.ERROR,
                )
            else:
                posted += 1
                self.message_user(
                    request,
                    f"{journal.reference} reversed by {reversal.reference}.",
                    messages.SUCCESS,
                )
        if posted:
            self.message_user(
                request, f"Posted {posted} reversing journal(s).",
                messages.SUCCESS,
            )


@admin.register(LedgerEntry)
class LedgerEntryAdmin(AppendOnlyAdmin):
    list_display = ("journal", "account", "membership", "debit", "credit",
                    "currency", "cooperative")
    list_filter = ("cooperative", "currency", "account__kind")
    search_fields = ("journal__reference", "account__code", "account__name",
                     "description")
    ordering = ("-id",)
    list_select_related = ("journal", "account", "membership", "cooperative")
