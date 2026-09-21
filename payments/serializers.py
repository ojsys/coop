from __future__ import annotations

from rest_framework import serializers

from payments.models import PaymentEvent, ProviderAccount


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
