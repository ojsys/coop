"""
Admin for loan products, loans, repayment schedules and repayments.

Loan money movement (disbursement, repayment allocation) posts balanced journals
through the ledger, so the fields those services own — the schedule split, the
posted journal, amounts already paid — are read-only here. Editing them by hand
would leave the loan book out of step with the accounts.
"""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
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


class LoanAdminForm(forms.ModelForm):
    """Keeps the ``status`` field away from the two transitions that move money.

    Every other status change is a label. Crossing into or out of **disbursed**
    is not: the disbursement posts a journal, builds the repayment schedule and
    stamps ``disbursed_at``. Typing the status by hand does none of that, and
    leaves the loan book contradicting the accounts — a loan that says the
    principal never left while the ledger says it did, or the reverse.

    So those two transitions are refused here and routed to the actions below,
    which go through the loan services and post a proper reversal. The field
    stays editable for everything else.
    """

    class Meta:
        model = Loan
        fields = "__all__"

    def clean_status(self):
        status = self.cleaned_data["status"]
        if self.instance.pk is None:
            return status

        # ``_post_clean`` applies cleaned_data to the instance *after* this runs,
        # so self.instance still holds what is in the database.
        was = self.instance.status
        disbursed = Loan.Status.DISBURSED

        if was == disbursed and status != disbursed:
            raise ValidationError(
                "This loan has been disbursed, so money has moved. Changing the "
                "status here would leave the ledger showing a payment that the "
                "loan no longer claims. Use the “Reverse disbursement” action on "
                "the loan list instead — it posts a reversing journal, deletes "
                "the repayment schedule and records who did it.")

        if status == disbursed and was != disbursed:
            raise ValidationError(
                "Marking a loan disbursed by hand moves no money and builds no "
                "repayment schedule, so nothing would ever fall due. Disburse it "
                "from the console (Loans → Review & pay), which posts the "
                "journal and notifies the member.")

        return status


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
    form = LoanAdminForm
    actions = ("reverse_disbursement", "return_to_pending", "reopen_owing")
    # These post or depend on ledger entries, so they follow the same rule as
    # editing: 2FA first.
    two_factor_actions = ("reverse_disbursement", "return_to_pending",
                          "reopen_owing")

    @admin.action(description="Reopen if marked repaid but still owing")
    def reopen_owing(self, request, queryset):
        """Return a loan marked repaid to disbursed when money is still owed.

        The status form cannot do this — it refuses any hand edit into
        "disbursed" — yet a loan stuck on "Repaid" with a balance blocks the
        member from paying. Moves no money; loans that are genuinely settled
        are left alone.
        """
        from loans.services import reopen_repaid_loan

        reopened = [loan.pk for loan in queryset
                    if reopen_repaid_loan(loan, actor=request.user)]
        if reopened:
            self.message_user(
                request, f"Reopened loan(s) {', '.join(map(str, reopened))} as "
                f"disbursed. The members can repay them again.",
                messages.SUCCESS)
        skipped = queryset.count() - len(reopened)
        if skipped:
            self.message_user(
                request, f"Left {skipped} loan(s) alone: not marked repaid, or "
                f"nothing is owed.", messages.INFO)

    @admin.action(description="Reverse disbursement (posts a ledger reversal)")
    def reverse_disbursement(self, request, queryset):
        """Undo a disbursement properly: reverse the journal, drop the schedule.

        For a loan paid in error, or marked disbursed when the money never
        actually went out.
        """
        from loans.services import LoanError, unwind_disbursement

        done, failed = 0, 0
        for loan in queryset:
            try:
                unwind_disbursement(
                    loan, reason="Reversed by an operator in the admin.",
                    actor=request.user)
            except LoanError as exc:
                self.message_user(request, f"Loan {loan.pk}: {exc}",
                                  messages.WARNING)
                failed += 1
                continue
            done += 1

        if done:
            self.message_user(
                request,
                f"Reversed {done} disbursement(s). Each reversal is posted as a "
                f"mirror journal — the original entry is left in place — and the "
                f"loan is back to approved.",
                messages.SUCCESS)
        if not done and not failed:
            self.message_user(request, "Nothing selected.", messages.INFO)

    @admin.action(description="Return to pending (un-approve)")
    def return_to_pending(self, request, queryset):
        """Send a loan back to the approval queue.

        A disbursed loan is refused rather than unwound silently: that would
        reverse a ledger entry as a side effect of a status change, which is
        exactly the kind of quiet money movement this admin is built to prevent.
        Reverse the disbursement first, then do this.
        """
        from loans.services import LoanError
        from loans.services import return_to_pending as _return

        done = 0
        for loan in queryset:
            try:
                _return(loan, reason="Returned to pending by an operator.",
                        actor=request.user)
            except LoanError as exc:
                self.message_user(request, f"Loan {loan.pk}: {exc}",
                                  messages.WARNING)
                continue
            done += 1

        if done:
            self.message_user(
                request,
                f"{done} loan(s) returned to pending. The previous approval has "
                f"been cleared, so each needs deciding again.",
                messages.SUCCESS)

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
    # The journal is written by the repayment service, and status follows the
    # ledger: typing "reversed" here would leave the money posted.
    readonly_fields = ("journal", "status", "reversal_journal",
                       "reversal_reason", "reversed_at", "created_at",
                       "updated_at")

    actions = ("recheck_selected", "reverse_selected")
    # Both post to the ledger.
    two_factor_actions = ("recheck_selected", "reverse_selected")

    @admin.action(description="Recheck online payment with the provider")
    def recheck_selected(self, request, queryset):
        """Make each online repayment agree with what Paystack received.

        Works in both directions:

        * **Pending** — the member paid and closed the tab before returning, so
          the money was taken but nothing posted. Settled if Paystack has it.
        * **Confirmed** — posted although Paystack never received the money
          (an officer's Confirm used to post any pending row). Reversed, which
          puts the loan back to the member to repay.

        Bank-transfer claims and cash are skipped: Paystack never sees them.
        Use "Reverse repayment" for those.
        """
        from payments.providers import PaymentInitError
        from payments.services import (PaymentNotReceived,
                                       provider_is_simulated,
                                       recheck_settled_repayment,
                                       verify_loan_payment)

        if provider_is_simulated() and not settings.DEBUG:
            self.message_user(
                request,
                "No live payment key is configured, so the provider would "
                "report every payment as paid. Nothing was changed.",
                messages.ERROR)
            return

        settled = waiting = skipped = kept = 0
        for repayment in queryset.select_related("cooperative", "loan"):
            reference = repayment.psp_reference
            if (repayment.channel != LoanRepayment.Channel.PSP
                    or not reference
                    or repayment.status == LoanRepayment.Status.REVERSED):
                skipped += 1
                continue

            if repayment.status == LoanRepayment.Status.CONFIRMED:
                try:
                    _, outcome = recheck_settled_repayment(
                        repayment, actor=request.user)
                except PaymentInitError as exc:
                    self.message_user(
                        request, f"{reference}: could not reach the provider "
                        f"— {exc}", messages.ERROR)
                    continue
                if outcome == "reversed":
                    repayment.refresh_from_db()
                    self.message_user(
                        request, f"{reference}: confirmed → reversed. "
                        f"{repayment.reversal_reason} Loan "
                        f"#{repayment.loan_id} is open for repayment again.",
                        messages.WARNING)
                elif outcome == "short":
                    self.message_user(
                        request, f"{reference}: Paystack received less than "
                        f"the {repayment.amount:,.2f} posted. Not reversed "
                        f"automatically — check it in the Paystack dashboard.",
                        messages.WARNING)
                else:
                    kept += 1
                continue

            try:
                result = verify_loan_payment(repayment.cooperative, reference)
            except PaymentNotReceived as exc:
                waiting += 1
                if exc.provider_status == "partial":
                    self.message_user(request, f"{reference}: {exc}",
                                      messages.WARNING)
                continue
            except PaymentInitError as exc:
                self.message_user(
                    request, f"{reference}: could not reach the provider — "
                    f"{exc}", messages.ERROR)
                continue

            if result is None:
                settled += 1
                self.message_user(
                    request, f"{reference}: paid, but the loan was already "
                    f"fully repaid, so nothing was posted. Refund the member.",
                    messages.WARNING)
            elif result.status == LoanRepayment.Status.CONFIRMED:
                settled += 1
                self.message_user(
                    request, f"{reference}: pending → confirmed. "
                    f"{result.amount:,.2f} posted to loan #{result.loan_id}.",
                    messages.SUCCESS)
            else:
                waiting += 1

        if kept:
            self.message_user(
                request, f"{kept} confirmed repayment(s) match a successful "
                f"Paystack payment — left as they are.", messages.INFO)
        if waiting:
            self.message_user(
                request, f"{waiting} repayment(s) are not paid at the provider "
                f"yet, so they stay pending.", messages.INFO)
        if skipped:
            self.message_user(
                request, f"Skipped {skipped} repayment(s) that are not online "
                f"payments or are already reversed.", messages.INFO)

    @admin.action(description="Reverse repayment (posts a ledger reversal)")
    def reverse_selected(self, request, queryset):
        """Reverse a repayment posted in error, whatever its channel.

        For what Paystack cannot judge — a cash entry or a reported transfer
        that never reached the bank. Online repayments are better rechecked,
        which reverses them only if Paystack agrees nothing arrived.
        """
        from loans.services import LoanError, reverse_repayment

        done = 0
        for repayment in queryset.select_related("loan"):
            try:
                reverse_repayment(
                    repayment, actor=request.user,
                    reason="Reversed by an operator: no payment was received.")
            except LoanError as exc:
                self.message_user(request, f"Repayment {repayment.pk}: {exc}",
                                  messages.WARNING)
                continue
            done += 1
        if done:
            self.message_user(
                request, f"Reversed {done} repayment(s). Each loan's balance "
                f"and schedule are restored, and a repaid loan is open for "
                f"repayment again.", messages.SUCCESS)
