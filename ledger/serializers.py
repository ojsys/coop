from __future__ import annotations

from rest_framework import serializers

from ledger.models import Account, Journal, LedgerEntry


class AccountSerializer(serializers.ModelSerializer):
    balance = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True,
    )

    class Meta:
        model = Account
        fields = ["id", "code", "name", "kind", "system", "balance"]


class LedgerEntrySerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    reference = serializers.CharField(source="journal.reference", read_only=True)
    occurred_at = serializers.DateTimeField(
        source="journal.occurred_at", read_only=True,
    )

    class Meta:
        model = LedgerEntry
        fields = ["id", "reference", "occurred_at", "account", "account_code",
                  "account_name", "membership", "debit", "credit", "currency",
                  "description"]


class JournalSerializer(serializers.ModelSerializer):
    entries = LedgerEntrySerializer(many=True, read_only=True)
    is_reversal = serializers.BooleanField(read_only=True)
    is_reversed = serializers.BooleanField(read_only=True)

    class Meta:
        model = Journal
        fields = ["id", "reference", "memo", "occurred_at", "posted_at",
                  "reversal_of", "is_reversal", "is_reversed", "entries"]
