"""Financial-integrity tests — the non-negotiable core (PRD §17)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.context import use_tenant
from ledger.models import Account, LedgerEntry, ImmutableLedgerError
from ledger.services import (
    Line, LedgerError, UnbalancedJournalError, account_balance, post_journal,
    reverse_journal,
)

pytestmark = pytest.mark.django_db


def _accounts(coop):
    cash = Account.all_objects.get(cooperative=coop, code="1000")
    funds = Account.all_objects.get(cooperative=coop, code="2000")
    return cash, funds


def test_posting_balances_debits_equal_credits(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        journal = post_journal(
            cooperative=coop, reference="J1",
            lines=[Line(account=cash, debit="5000"),
                   Line(account=funds, credit="5000")],
        )
    agg = {"d": Decimal("0"), "c": Decimal("0")}
    for e in LedgerEntry.all_objects.filter(journal=journal):
        agg["d"] += e.debit
        agg["c"] += e.credit
    assert agg["d"] == agg["c"] == Decimal("5000.00")


def test_unbalanced_journal_is_rejected(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        with pytest.raises(UnbalancedJournalError):
            post_journal(
                cooperative=coop, reference="BAD",
                lines=[Line(account=cash, debit="5000"),
                       Line(account=funds, credit="4000")],
            )
        # Nothing was written.
        assert LedgerEntry.objects.count() == 0


def test_single_line_journal_rejected(coop):
    cash, _ = _accounts(coop)
    with use_tenant(coop):
        with pytest.raises(LedgerError):
            post_journal(cooperative=coop, reference="ONE",
                         lines=[Line(account=cash, debit="10")])


def test_line_cannot_be_both_debit_and_credit(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        with pytest.raises(LedgerError):
            post_journal(
                cooperative=coop, reference="BOTH",
                lines=[Line(account=cash, debit="10", credit="10"),
                       Line(account=funds, credit="10")],
            )


def test_derived_account_balances(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        post_journal(cooperative=coop, reference="A",
                     lines=[Line(account=cash, debit="5000"),
                            Line(account=funds, credit="5000")])
        post_journal(cooperative=coop, reference="B",
                     lines=[Line(account=cash, debit="3000"),
                            Line(account=funds, credit="3000")])
    # Asset (debit-normal) grows on debits; liability grows on credits.
    assert account_balance(cash) == Decimal("8000.00")
    assert account_balance(funds) == Decimal("8000.00")


def test_ledger_entries_are_immutable(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        journal = post_journal(cooperative=coop, reference="IMM",
                               lines=[Line(account=cash, debit="1000"),
                                      Line(account=funds, credit="1000")])
    entry = LedgerEntry.all_objects.filter(journal=journal).first()
    entry.debit = Decimal("999999")
    with pytest.raises(ImmutableLedgerError):
        entry.save()
    with pytest.raises(ImmutableLedgerError):
        entry.delete()
    with pytest.raises(ImmutableLedgerError):
        journal.delete()


def test_reversal_preserves_original_and_zeroes_balance(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        original = post_journal(cooperative=coop, reference="C",
                                lines=[Line(account=cash, debit="5000"),
                                       Line(account=funds, credit="5000")])
        assert account_balance(cash) == Decimal("5000.00")

        reversal = reverse_journal(original)

        # Original is untouched; net effect is zero after reversal.
        assert LedgerEntry.all_objects.filter(journal=original).count() == 2
        assert reversal.reversal_of_id == original.id
        assert account_balance(cash) == Decimal("0.00")
        assert account_balance(funds) == Decimal("0.00")


def test_double_reversal_is_rejected(coop):
    cash, funds = _accounts(coop)
    with use_tenant(coop):
        original = post_journal(cooperative=coop, reference="D",
                                lines=[Line(account=cash, debit="10"),
                                       Line(account=funds, credit="10")])
        reverse_journal(original)
        with pytest.raises(LedgerError):
            reverse_journal(original)
