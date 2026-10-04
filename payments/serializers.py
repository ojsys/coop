from __future__ import annotations

from rest_framework import serializers

from payments.models import (PaymentEvent, Payout, ProviderAccount,
                             WalletTopUp)


class PayoutSerializer(serializers.ModelSerializer):
    """An outbound transfer. Read-only: a payout is the record of money that
    has already been handed to the provider."""

    kind_display = serializers.CharField(source="get_kind_display",
                                         read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    summary = serializers.CharField(source="describe", read_only=True)
    needs_recheck = serializers.BooleanField(read_only=True)
    is_settled = serializers.BooleanField(read_only=True)
    requested_by_name = serializers.CharField(
        source="requested_by.full_name", read_only=True, default=None)

    class Meta:
        model = Payout
        fields = ["id", "kind", "kind_display", "object_id", "amount",
                  "currency", "status", "status_display", "summary",
                  "needs_recheck", "is_settled", "destination_bank_name",
                  "destination_bank_code", "destination_account_no",
                  "destination_account_name", "reference", "transfer_code",
                  "reason", "failure_reason", "journal", "requested_by",
                  "requested_by_name", "sent_at", "settled_at", "created_at"]
        read_only_fields = fields


class WalletTopUpSerializer(serializers.ModelSerializer):
    """A society funding its disbursement wallet.

    All three amounts are exposed because they genuinely differ: the officer
    paid ``amount``, the provider kept ``fee``, and ``net_amount`` is what the
    wallet was actually credited. Showing only the first would make the balance
    look wrong to whoever reconciles it.
    """

    initiated_by_name = serializers.CharField(
        source="initiated_by.full_name", read_only=True, default=None)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    is_confirmed = serializers.BooleanField(read_only=True)

    class Meta:
        model = WalletTopUp
        fields = ["id", "amount", "fee", "net_amount", "currency", "status",
                  "status_display", "is_confirmed", "provider",
                  "psp_reference", "initiated_by", "initiated_by_name",
                  "journal", "confirmed_at", "created_at"]
        # Everything is set by the top-up flow: the officer supplies only an
        # amount, and the fee and net come from the provider at confirmation.
        read_only_fields = fields


class ProviderAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProviderAccount
        fields = ["id", "provider", "subaccount_code", "bank_name",
                  "connected"]


class PaymentEventSerializer(serializers.ModelSerializer):
    member_no = serializers.CharField(
        source="matched_contribution.membership.member_no",
        read_only=True, default=None,
    )
    is_exception = serializers.BooleanField(read_only=True)

    class Meta:
        model = PaymentEvent
        fields = ["id", "provider", "event_id", "reference", "amount",
                  "currency", "status", "is_exception", "matched_contribution",
                  "member_no", "note", "received_at"]
