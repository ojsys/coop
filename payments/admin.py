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

from django.contrib import admin
from django.utils.html import format_html

from core.admin import TenantScopedModelAdmin, TwoFactorRequiredMixin
from payments.models import (Bank, PaymentEvent, Payout, ProviderAccount,
                             WalletTopUp)


@admin.register(Payout)
class PayoutAdmin(TwoFactorRequiredMixin, TenantScopedModelAdmin):
    """Money sent out — read-only.

    A payout row is the record of a transfer that has already been handed to
    the provider. Editing its status by hand would make the ledger disagree
    with what the provider actually did; use the officer-facing recheck, which
    asks the provider and reconciles.
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
    """

    list_display = ("psp_reference", "cooperative", "amount", "fee",
                    "net_amount", "status", "confirmed_at")
    list_filter = ("status", "provider", "cooperative")
    search_fields = ("psp_reference",)
    date_hierarchy = "created_at"
    list_select_related = ("cooperative", "initiated_by")
    readonly_fields = ("cooperative", "amount", "fee", "net_amount",
                       "currency", "status", "provider", "psp_reference",
                       "initiated_by", "journal", "confirmed_at",
                       "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Bank)
class BankAdmin(admin.ModelAdmin):
    """The provider's bank catalogue — read-only.

    A plain ModelAdmin, not TenantScopedModelAdmin: the catalogue is the same
    for every cooperative, so this model has no ``cooperative`` field to scope
    by.

    Read-only because the rows are pulled from the provider by
    ``manage.py refresh_banks``. A hand-typed bank code would not fail
    visibly — it would quietly point a cooperative's settlement subaccount, or
    a member's payout, at the wrong bank. Inactive rows are kept on purpose:
    records may still reference a code the provider has dropped.
    """

    list_display = ("name", "code", "currency", "provider", "active")
    list_filter = ("active", "provider", "currency")
    search_fields = ("name", "code", "slug")
    ordering = ("name",)
    readonly_fields = ("code", "name", "slug", "currency", "provider",
                       "active", "created_at", "updated_at")

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
