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
from payments.models import PaymentEvent, ProviderAccount


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
