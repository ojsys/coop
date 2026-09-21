"""
Financial statements derived from the immutable double-entry ledger:
trial balance, income statement and balance sheet. Everything is computed from
account balances — nothing is stored — so the statements can never drift from
the ledger.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from ledger.models import Account, LedgerEntry
from ledger.services import account_balance

ZERO = Decimal("0.00")


def _accounts(cooperative):
    return Account.all_objects.filter(cooperative=cooperative).order_by("code")


def trial_balance(cooperative) -> dict:
    """Every account with its balance on the correct side; totals must match."""
    rows = []
    total_debit = ZERO
    total_credit = ZERO
    for acc in _accounts(cooperative):
        agg = LedgerEntry.all_objects.filter(account=acc).aggregate(
            d=Sum("debit"), c=Sum("credit"))
        net = (agg["d"] or ZERO) - (agg["c"] or ZERO)  # debit-basis
        if net == 0:
            continue
        debit = net if net > 0 else ZERO
        credit = -net if net < 0 else ZERO
        total_debit += debit
        total_credit += credit
        rows.append({
            "code": acc.code, "name": acc.name, "kind": acc.kind,
            "debit": debit, "credit": credit,
        })
    return {
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": total_debit == total_credit,
    }


def _by_kind(cooperative, kind):
    accounts = _accounts(cooperative).filter(kind=kind)
    lines = [
        {"code": a.code, "name": a.name, "amount": account_balance(a)}
        for a in accounts
    ]
    total = sum((line["amount"] for line in lines), ZERO)
    return lines, total


def income_statement(cooperative) -> dict:
    income, income_total = _by_kind(cooperative, Account.Kind.INCOME)
    expense, expense_total = _by_kind(cooperative, Account.Kind.EXPENSE)
    surplus = income_total - expense_total
    return {
        "income": income,
        "income_total": income_total,
        "expense": expense,
        "expense_total": expense_total,
        "surplus": surplus,
    }


def balance_sheet(cooperative) -> dict:
    assets, assets_total = _by_kind(cooperative, Account.Kind.ASSET)
    liabilities, liabilities_total = _by_kind(cooperative, Account.Kind.LIABILITY)
    equity, equity_total = _by_kind(cooperative, Account.Kind.EQUITY)
    # Retained surplus (income − expense) belongs to members' equity.
    surplus = income_statement(cooperative)["surplus"]
    total_equity = equity_total + surplus
    return {
        "assets": assets,
        "assets_total": assets_total,
        "liabilities": liabilities,
        "liabilities_total": liabilities_total,
        "equity": equity,
        "retained_surplus": surplus,
        "total_equity_and_liabilities": liabilities_total + total_equity,
        "balanced": assets_total == liabilities_total + total_equity,
    }
