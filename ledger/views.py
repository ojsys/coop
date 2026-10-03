from __future__ import annotations

from decimal import InvalidOperation

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from rest_framework.response import Response

from core.context import get_current_cooperative
from core.permissions import IsPrivilegedOfficerOrReadOnly
from core.views import TenantScopedViewMixin
from ledger.models import Account, InternalTransfer, Journal, LedgerEntry
from ledger.serializers import (
    AccountSerializer, InternalTransferSerializer, JournalSerializer,
    LedgerEntrySerializer,
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


class InternalTransferViewSet(TenantScopedViewMixin, mixins.ListModelMixin,
                              mixins.RetrieveModelMixin,
                              viewsets.GenericViewSet):
    """`/internal-transfers/` — the cooperative moving its own money about.

    Fund movement is not automatic: an officer makes the real transfer at the
    bank, then records it here so the ledger matches reality. Unlike a member
    withdrawal this needs one officer, not two — both legs are the cooperative's
    own asset accounts, so nothing leaves and no member balance moves. The
    asset-only rule lives in the service and is what keeps this from becoming a
    way to debit Member Funds without approval.

    Creation goes through ledger.services.record_internal_transfer rather than
    the serializer's save(): the guards and the journal are the whole point, and
    a plain ModelSerializer create would write a transfer row describing a
    posting that never happened.
    """

    serializer_class = InternalTransferSerializer
    permission_classes = [IsPrivilegedOfficerOrReadOnly]

    def get_queryset(self):
        return InternalTransfer.objects.select_related(
            "from_account", "to_account", "recorded_by", "journal")

    def create(self, request, *args, **kwargs):
        from ledger.services import TransferError, record_internal_transfer

        cooperative = get_current_cooperative()
        accounts = Account.objects.in_bulk([
            request.data.get("from_account"), request.data.get("to_account"),
        ])
        source = accounts.get(_as_int(request.data.get("from_account")))
        target = accounts.get(_as_int(request.data.get("to_account")))
        if source is None or target is None:
            return Response(
                {"detail": "Choose both accounts from this cooperative's "
                           "chart of accounts."},
                status=400)

        try:
            transfer = record_internal_transfer(
                cooperative=cooperative,
                from_account=source,
                to_account=target,
                amount=request.data.get("amount"),
                occurred_at=request.data.get("occurred_at") or None,
                note=request.data.get("note", ""),
                recorded_by=request.user,
            )
        except TransferError as exc:
            # TransferError subclasses LedgerError; without catching it this
            # would surface as a 500 rather than the explanation it carries.
            return Response({"detail": str(exc)}, status=400)
        except (InvalidOperation, TypeError):
            return Response({"detail": "Enter a valid amount."}, status=400)

        return Response(self.get_serializer(transfer).data, status=201)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
