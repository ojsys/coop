from __future__ import annotations

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from core.views import TenantScopedViewMixin
from ledger.models import Account, Journal, LedgerEntry
from ledger.serializers import (
    AccountSerializer, JournalSerializer, LedgerEntrySerializer,
)


class AccountViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                     mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Chart of accounts with derived balances (read-only)."""

    serializer_class = AccountSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Account.objects.all()


class JournalViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                     mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Posted journals with their balanced lines (read-only, append-only)."""

    serializer_class = JournalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Journal.objects.prefetch_related("entries__account")


class LedgerEntryViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                         viewsets.GenericViewSet):
    """The raw ledger — every debit/credit line (read-only, append-only)."""

    serializer_class = LedgerEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = LedgerEntry.objects.select_related("journal", "account")
        account = self.request.query_params.get("account")
        if account:
            qs = qs.filter(account_id=account)
        return qs

    @action(detail=False, methods=["get"])
    def export(self, request):
        from reports.csv_export import ledger_entries_csv

        return ledger_entries_csv(self.get_queryset())
