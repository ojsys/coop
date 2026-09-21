"""
Admin for loan products, loans, repayment schedules and repayments.

Loan money movement (disbursement, repayment allocation) posts balanced journals
through the ledger, so the fields those services own — the schedule split, the
posted journal, amounts already paid — are read-only here. Editing them by hand
would leave the loan book out of step with the accounts.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib import admin
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from core.admin import (TenantScopedModelAdmin, TenantScopedTabularInline,
                        TwoFactorRequiredMixin)
from loans.models import Loan, LoanProduct, LoanRepayment, RepaymentInstalment

_MONEY = DecimalField(max_digits=16, decimal_places=2)
_ZERO = Value(Decimal("0.00"), output_field=_MONEY)


@admin.register(LoanProduct)
class LoanProductAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("name", "cooperative", "interest_rate", "max_amount",
                    "max_term_months", "active")
    list_filter = ("active", "cooperative")
    search_fields = ("name",)
    ordering = ("cooperative", "name")
    autocomplete_fields = ("cooperative",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("cooperative",)


class RepaymentInstalmentInline(TenantScopedTabularInline):
    model = RepaymentInstalment
    extra = 0
    can_delete = False
    fields = ("sequence", "due_date", "amount_due", "principal_component",
              "interest_component", "amount_paid", "status", "is_overdue")
    readonly_fields = fields
    ordering = ("sequence",)

    @admin.display(boolean=True, description="Overdue")
    def is_overdue(self, obj):
        return obj.is_overdue

    def has_add_permission(self, request, obj=None):
        # The schedule is built at disbursement.
        return False


class LoanRepaymentInline(TenantScopedTabularInline):
    model = LoanRepayment
    extra = 0
    can_delete = False
    fields = ("amount", "channel", "status", "psp_reference", "note",
              "journal", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Loan)
class LoanAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("id", "membership", "product", "principal", "interest_rate",
                    "term_months", "status", "outstanding", "cooperative")
    list_filter = ("status", "cooperative", "product", "created_at")
    search_fields = ("membership__member_no", "membership__user__full_name",
                     "purpose")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "membership", "product", "decided_by")
    list_select_related = ("membership", "product", "cooperative")
    readonly_fields = ("decided_by", "decided_at", "disbursed_at", "interest",
                       "total_repayable", "repaid_amount", "outstanding",
                       "monthly_instalment", "created_at", "updated_at")
    inlines = (RepaymentInstalmentInline, LoanRepaymentInline)

    fieldsets = (
        (None, {
            "fields": ("cooperative", "membership", "product", "status"),
        }),
        ("Terms", {
            "fields": ("principal", "interest_rate", "term_months", "purpose"),
        }),
        ("Derived figures", {
            "description": "Computed from the amortisation schedule and the "
                           "ledger — never stored.",
            "fields": ("interest", "total_repayable", "monthly_instalment",
                       "repaid_amount", "outstanding"),
        }),
        ("Decision & disbursement", {
            "fields": ("decided_by", "decided_at", "disbursed_at"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    def get_queryset(self, request):
        # Loan.repaid_amount queries the repayments per call; aggregate once.
        # (interest/total_repayable are pure Python amortisation — no query.)
        confirmed = Q(repayments__status=LoanRepayment.Status.CONFIRMED)
        return super().get_queryset(request).annotate(
            _repaid=Coalesce(Sum("repayments__amount", filter=confirmed),
                             _ZERO, output_field=_MONEY),
        )

    def get_fieldsets(self, request, obj=None):
        # There is nothing to derive from before the loan exists, and the
        # amortisation schedule cannot be built from a blank principal.
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None:
            return tuple(fs for fs in fieldsets if fs[0] != "Derived figures")
        return fieldsets

    @admin.display(description="Interest")
    def interest(self, obj):
        return "—" if obj.pk is None else obj.interest

    @admin.display(description="Total repayable")
    def total_repayable(self, obj):
        return "—" if obj.pk is None else obj.total_repayable

    @admin.display(description="Repaid")
    def repaid_amount(self, obj):
        if obj.pk is None:
            return "—"
        repaid = getattr(obj, "_repaid", None)
        return obj.repaid_amount if repaid is None else repaid

    @admin.display(description="Outstanding")
    def outstanding(self, obj):
        if obj.pk is None:
            return "—"
        repaid = getattr(obj, "_repaid", None)
        if repaid is None:         # detail page: no annotation, fall back
            return obj.outstanding
        # Mirrors Loan.outstanding, reusing the aggregated repayment total.
        if obj.status in (Loan.Status.DISBURSED, Loan.Status.REPAID):
            return obj.total_repayable - repaid
        return Decimal("0.00")

    @admin.display(description="Monthly instalment")
    def monthly_instalment(self, obj):
        return "—" if obj.pk is None else obj.monthly_instalment


@admin.register(RepaymentInstalment)
class RepaymentInstalmentAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("loan", "sequence", "due_date", "amount_due", "amount_paid",
                    "status", "is_overdue", "cooperative")
    list_filter = ("status", "cooperative", "due_date")
    search_fields = ("loan__membership__member_no",
                     "loan__membership__user__full_name")
    date_hierarchy = "due_date"
    ordering = ("loan", "sequence")
    autocomplete_fields = ("cooperative", "loan")
    list_select_related = ("loan", "cooperative")

    @admin.display(boolean=True, description="Overdue")
    def is_overdue(self, obj):
        return obj.is_overdue


@admin.register(LoanRepayment)
class LoanRepaymentAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("loan", "amount", "channel", "status", "psp_reference",
                    "created_at", "cooperative")
    list_filter = ("status", "channel", "cooperative", "created_at")
    search_fields = ("psp_reference", "note", "loan__membership__member_no",
                     "loan__membership__user__full_name")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    autocomplete_fields = ("cooperative", "loan")
    list_select_related = ("loan", "cooperative")
    # The journal is written by the repayment service.
    readonly_fields = ("journal", "created_at", "updated_at")
