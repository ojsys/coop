from __future__ import annotations

from rest_framework import serializers

from ledger.models import Account, InternalTransfer, Journal, LedgerEntry


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


class InternalTransferSerializer(serializers.ModelSerializer):
    """The cooperative's own money moved between its own accounts.

    Write fields are only the two accounts, the amount, the date it really
    happened and a note. The journal is produced by the service, never supplied:
    a client that could name its own journal could decouple the record from the
    posting it is supposed to describe.
    """

    from_account_code = serializers.CharField(source="from_account.code",
                                              read_only=True)
    from_account_name = serializers.CharField(source="from_account.name",
                                              read_only=True)
    to_account_code = serializers.CharField(source="to_account.code",
                                            read_only=True)
    to_account_name = serializers.CharField(source="to_account.name",
                                            read_only=True)
    recorded_by_name = serializers.CharField(
        source="recorded_by.full_name", read_only=True, default=None)
    summary = serializers.CharField(source="describe", read_only=True)

    class Meta:
        model = InternalTransfer
        fields = ["id", "from_account", "from_account_code",
                  "from_account_name", "to_account", "to_account_code",
                  "to_account_name", "amount", "occurred_at", "note",
                  "recorded_by", "recorded_by_name", "journal", "summary",
                  "created_at"]
        read_only_fields = ["recorded_by", "journal"]
