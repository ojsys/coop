"""Contribution recording → ledger posting → derived member balance."""
from __future__ import annotations

from decimal import Decimal

import pytest

from contributions.models import Contribution
from contributions.services import (
    ContributionError, record_contribution, reverse_contribution,
)
from core.context import use_tenant
from ledger.models import Account
from ledger.services import account_balance, member_balance, member_statement

pytestmark = pytest.mark.django_db


def test_record_contribution_posts_balanced_journal(coop, member, dues_type):
    with use_tenant(coop):
        contribution = record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", channel="psp", psp_reference="PSTK_1",
        )
    assert contribution.status == Contribution.Status.CONFIRMED
    from ledger.models import LedgerEntry
    entries = LedgerEntry.all_objects.filter(journal=contribution.journal)
    assert sum(e.debit for e in entries) == sum(e.credit for e in entries)

    bank = Account.all_objects.get(cooperative=coop, code="1010")
    assert account_balance(bank) == Decimal("5000.00")
    assert member_balance(member) == Decimal("5000.00")


def test_member_balance_and_statement_accumulate(coop, member, dues_type):
    with use_tenant(coop):
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="5000",
                            channel="cash")
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="30000",
                            channel="transfer")
        assert member_balance(member) == Decimal("35000.00")

        statement = member_statement(member)
        assert len(statement) == 2
        # Running balance ends at the total.
        assert statement[-1]["balance"] == Decimal("35000.00")


def test_reversing_contribution_zeroes_member_balance(coop, member, dues_type):
    with use_tenant(coop):
        contribution = record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", channel="cash",
        )
        assert member_balance(member) == Decimal("5000.00")

        reverse_contribution(contribution)
        contribution.refresh_from_db()

    assert contribution.status == Contribution.Status.REVERSED
    assert member_balance(member) == Decimal("0.00")


def test_negative_amount_rejected(coop, member, dues_type):
    with use_tenant(coop):
        with pytest.raises(ContributionError):
            record_contribution(cooperative=coop, membership=member,
                                contribution_type=dues_type, amount="-100",
                                channel="cash")
