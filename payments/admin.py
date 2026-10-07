"""
Admin for PSP subaccounts and inbound webhook events.

``PaymentEvent`` rows are ingested from providers, so they are not hand-authored
here: adding one is disabled and every ingested field is read-only. ``status``
and ``note`` stay editable because reconciliation exceptions (unmatched, partial,
duplicate) are resolved manually by an operator — and since that is a
money-reconciliation action, it requires 2FA.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.contrib import admin, messages
from django.utils.html import format_html

from core.admin import (TenantScopedModelAdmin, TwoFactorActionsMixin,
                        TwoFactorRequiredMixin)
from payments.models import (Bank, PaymentEvent, Payout, ProviderAccount,
                             WalletTopUp)


@admin.register(Payout)
class PayoutAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    """Money sent out — read-only.

    A payout row is the record of a transfer that has already been handed to
    the provider. Editing its status by hand would make the ledger disagree
    with what the provider actually did.

    The legitimate operation is therefore an *action*, not an edit: ask the
    provider what really happened and apply whatever it says. Without it this
    page showed a payout stuck on "pending" and offered no way to resolve it,
    which is the case support is called about.
    """

    list_display = ("reference", "kind", "cooperative", "amount", "status",
                    "destination_account_no", "sent_at", "settled_at")
    list_filter = ("status", "kind", "provider", "cooperative")
    search_fields = ("reference", "transfer_code", "destination_account_no",
                     "destination_bank_name")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("cooperative", "requested_by")
    readonly_fields = tuple(
        f.name for f in Payout._meta.fields
    ) + ("created_at", "updated_at")

    actions = ("recheck_selected",)
    # Reconciles money: a payout that turns out to have failed has its journal
    # reversed and its loan reverted.
    two_factor_actions = ("recheck_selected",)

    @admin.action(description="Recheck with the provider")
    def recheck_selected(self, request, queryset):
        """Ask the provider, and apply the answer through the webhook path.

        Deliberately the same ``recheck_payout`` the console calls, so the admin
        and the officer-facing screen cannot form different opinions about
        whether money moved.
        """
        from payments.providers import PaymentInitError
        from payments.services import recheck_payout

        asked = changed = skipped = 0
        for payout in queryset:
            if not payout.needs_recheck:
                # The provider has already given a final answer; asking again
                # would not change it.
                skipped += 1
                continue

            reference, before = payout.reference, payout.status
            try:
                payout = recheck_payout(payout, actor=request.user)
            except PaymentInitError as exc:
                self.message_user(
                    request, f"{reference}: could not reach the provider — {exc}",
                    messages.ERROR)
                continue

            asked += 1
            if payout.status != before:
                changed += 1
                self.message_user(
                    request,
                    f"{reference}: {before} → {payout.status}. Any ledger "
                    f"correction has been posted.",
                    messages.SUCCESS)

        if asked and not changed:
            self.message_user(
                request,
                f"Asked the provider about {asked} payout(s); it reports no "
                f"change yet.", messages.INFO)
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} payout(s) the provider has already settled "
                f"or failed — there is nothing left to ask.", messages.INFO)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(WalletTopUp)
class WalletTopUpAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    """Societies funding their disbursement wallets — read-only.

    A top-up is created by the officer's checkout and confirmed against the
    provider, which is also where the fee comes from. Editing any of the three
    amounts by hand would put the wallet balance out of step with the money
    actually held at the provider, and the wallet is what disbursements draw
    on. Corrections belong in the ledger, by reversal.

    What *can* be done is to ask the provider. A top-up used to be confirmed
    only when the officer came back through the checkout's return URL, so one
    who paid and closed the tab left a row stuck on "pending" while the money
    sat at the provider. The recheck action applies the provider's answer.
    """

    list_display = ("psp_reference", "cooperative", "amount", "fee",
                    "net_amount", "status", "created_at", "confirmed_at")
    list_filter = ("status", "provider", "cooperative")
    search_fields = ("psp_reference",)
    date_hierarchy = "created_at"
    list_select_related = ("cooperative", "initiated_by")
    readonly_fields = ("cooperative", "amount", "fee", "net_amount",
                       "currency", "status", "provider", "psp_reference",
                       "initiated_by", "journal", "confirmed_at",
                       "created_at", "updated_at")

    actions = ("recheck_selected",)
    # Credits the wallet the society lends from, so it is a money action.
    two_factor_actions = ("recheck_selected",)

    @admin.action(description="Recheck with the provider")
    def recheck_selected(self, request, queryset):
        """Ask the provider about each pending top-up and apply the answer.

        The same ``recheck_wallet_topup`` the officer's return from checkout
        and the webhook use, so the admin cannot credit a wallet the console
        would not.
        """
        from payments.models import WalletTopUp
        from payments.providers import PaymentInitError
        from payments.services import (WalletError, provider_is_simulated,
                                       recheck_wallet_topup)

        if provider_is_simulated() and not settings.DEBUG:
            self.message_user(
                request,
                "No live payment key is configured, so the provider would "
                "report every top-up as paid. Nothing was changed.",
                messages.ERROR)
            return

        asked = changed = skipped = 0
        for topup in queryset:
            if topup.status != WalletTopUp.Status.PENDING:
                skipped += 1
                continue

            reference = topup.psp_reference
            try:
                topup = recheck_wallet_topup(topup, actor=request.user)
            except (PaymentInitError, WalletError) as exc:
                self.message_user(
                    request, f"{reference}: could not be confirmed — {exc}",
                    messages.ERROR)
                continue

            asked += 1
            if topup.is_confirmed:
                changed += 1
                self.message_user(
                    request,
                    f"{reference}: pending → confirmed. {topup.net_amount:,.2f} "
                    f"credited to {topup.cooperative}'s wallet "
                    f"(fee {topup.fee:,.2f}).", messages.SUCCESS)
            elif topup.status == WalletTopUp.Status.FAILED:
                changed += 1
                self.message_user(
                    request, f"{reference}: pending → failed. Nothing was "
                    f"credited.", messages.WARNING)

        if asked > changed:
            self.message_user(
                request,
                f"{asked - changed} top-up(s) are still not paid at the "
                f"provider, so they stay pending.", messages.INFO)
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} top-up(s) that are already confirmed or "
                f"failed.", messages.INFO)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Bank)
class BankAdmin(TwoFactorActionsMixin, admin.ModelAdmin):
    """The provider's bank catalogue — read-only.

    A plain ModelAdmin, not TenantScopedModelAdmin: the catalogue is the same
    for every cooperative, so this model has no ``cooperative`` field to scope
    by.

    Read-only because the rows are pulled from the provider. A hand-typed bank
    code would not fail visibly — it would quietly point a cooperative's
    settlement subaccount, or a member's payout, at the wrong bank. Inactive
    rows are kept on purpose: records may still reference a code the provider
    has dropped.

    Refreshing is offered as an action rather than left to
    ``manage.py refresh_banks`` alone, because an empty or stale catalogue blocks
    every electronic payout — and needing shell access to fix that is a poor
    place for an operator to be. Pulling the list is idempotent.
    """

    list_display = ("name", "code", "currency", "provider", "active")
    list_filter = ("active", "provider", "currency")
    search_fields = ("name", "code", "slug")
    ordering = ("name",)
    readonly_fields = ("code", "name", "slug", "currency", "provider",
                       "active", "created_at", "updated_at")
    actions = ("refresh_catalogue",)
    # Payout destinations are resolved against these codes, so this is data money
    # depends on even though the pull itself moves none.
    two_factor_actions = ("refresh_catalogue",)

    @admin.action(description="Refresh the catalogue from the provider")
    def refresh_catalogue(self, request, queryset):
        """Pull the provider's bank list. Ignores the selection by design.

        The whole catalogue is replaced from the provider, so refreshing "the
        selected rows" would be a misleading promise — Django requires a
        selection to run an action, and this one simply does not use it.
        """
        from payments.models import Bank
        from payments.providers import PaymentInitError

        try:
            summary = Bank.refresh()
        except PaymentInitError as exc:
            self.message_user(request, f"Could not reach the provider: {exc}",
                              messages.ERROR)
            return

        if not summary["fetched"]:
            self.message_user(
                request,
                "No banks returned — there is no live provider key configured, "
                "so nothing was changed.", messages.WARNING)
            return

        self.message_user(
            request,
            f"Fetched {summary['fetched']} banks: {summary['created']} added, "
            f"{summary['updated']} updated, {summary['deactivated']} marked "
            f"inactive. Codes already in use are never deleted.",
            messages.SUCCESS)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProviderAccount)
class ProviderAccountAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("cooperative", "provider", "subaccount_code", "bank_name",
                    "connected")
    list_filter = ("provider", "connected", "cooperative")
    search_fields = ("subaccount_code", "bank_name")
    autocomplete_fields = ("cooperative",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("cooperative",)


@admin.register(PaymentEvent)
class PaymentEventAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    list_display = ("reference", "provider", "amount", "currency", "status",
                    "is_exception", "cooperative", "received_at")
    list_filter = ("status", "provider", "currency", "cooperative",
                   "received_at")
    search_fields = ("reference", "event_id", "note")
    date_hierarchy = "received_at"
    ordering = ("-received_at", "-id")
    autocomplete_fields = ("matched_contribution",)
    list_select_related = ("cooperative", "matched_contribution")
    # The raw payload is shown formatted instead of as an editable JSON blob.
    exclude = ("payload",)
    readonly_fields = ("cooperative", "provider", "event_id", "reference",
                       "amount", "currency", "matched_contribution",
                       "payload_pretty", "received_at", "created_at",
                       "updated_at")

    @admin.display(boolean=True, description="Exception")
    def is_exception(self, obj):
        return obj.is_exception

    @admin.display(description="Payload")
    def payload_pretty(self, obj):
        if not obj.payload:
            return "—"
        return format_html(
            '<pre style="white-space:pre-wrap;margin:0">{}</pre>',
            json.dumps(obj.payload, indent=2, sort_keys=True, default=str),
        )

    def has_add_permission(self, request):
        # Events arrive from the provider's webhook, never by hand.
        return False
