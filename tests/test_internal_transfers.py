"""
The cooperative moving its own money about.

Banking a cash float, or drawing cash from the bank, is not a payment to anyone
— it is the same money in a different place. So unlike a withdrawal this needs
one privileged officer rather than two: nothing leaves the cooperative and no
member's balance moves.

That distinction is only safe because of one guard, and it is the guard most of
these tests exist to pin down: **both legs must be asset accounts**. Without it
this endpoint would be a way to debit Member Funds — reducing what the
cooperative owes its members — with a single officer's say-so and no approval,
which is exactly the control the withdrawal flow exists to enforce.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from audit.models import AuditLog
from contributions.models import Contribution
from contributions.services import record_contribution
from core.context import use_tenant
from ledger.models import Account, InternalTransfer, LedgerEntry
from ledger.services import TransferError, account_balance, member_balance, \
    record_internal_transfer

pytestmark = pytest.mark.django_db


@pytest.fixture
def funded(coop, member, dues_type):
    """10,000 of real cash in the cooperative's 1000 Cash account.

    A cash contribution debits 1000 and credits 2000, so this also gives the
    member a 10,000 savings balance — useful, because several tests below assert
    that an internal transfer leaves that balance untouched.
    """
    with use_tenant(coop):
        record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=Decimal("10000.00"), channel=Contribution.Channel.CASH,
        )
    return member


def _accounts(coop):
    cash = Account.all_objects.get(cooperative=coop, code="1000")
    bank = Account.all_objects.get(cooperative=coop, code="1010")
    member_funds = Account.all_objects.get(cooperative=coop, code="2000")
    return cash, bank, member_funds


def _officer(coop, email, slug=Role.SECRETARY):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email,
                                        password="x")
        Membership.objects.create(
            user=user, member_no=email[:6],
            role=Role.objects.filter(slug=slug).first())
    return user


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _transfer(coop, source, target, amount, actor=None, **kw):
    with use_tenant(coop):
        return record_internal_transfer(
            cooperative=coop, from_account=source, to_account=target,
            amount=amount, recorded_by=actor, **kw)


# ── The happy path ──────────────────────────────────────────────────────────
def test_banking_the_cash_float_moves_it_between_asset_accounts(coop, funded):
    cash, bank, _ = _accounts(coop)
    officer = _officer(coop, "sec@imole.coop")

    transfer = _transfer(coop, cash, bank, "4000", officer,
                         note="Banked the Friday float")

    assert account_balance(cash) == Decimal("6000.00")
    assert account_balance(bank) == Decimal("4000.00")
    assert transfer.journal is not None


def test_the_journal_debits_the_destination_and_credits_the_source(coop, funded):
    """Money arriving at the bank is a debit to that asset; leaving cash, a credit."""
    cash, bank, _ = _accounts(coop)

    transfer = _transfer(coop, cash, bank, "4000")

    # all_objects, not journal.entries: LedgerEntry is tenant-scoped and the
    # reverse manager fails closed with no tenant bound, which is every line of
    # a test outside use_tenant().
    lines = {
        e.account.code: e
        for e in LedgerEntry.all_objects.filter(journal=transfer.journal)
    }
    assert lines["1010"].debit == Decimal("4000.00")
    assert lines["1000"].credit == Decimal("4000.00")


def test_a_transfer_does_not_touch_any_member_balance(coop, funded):
    """The whole justification for needing one officer instead of two."""
    cash, bank, _ = _accounts(coop)

    _transfer(coop, cash, bank, "4000")

    assert member_balance(funded) == Decimal("10000.00")


def test_it_is_recorded_in_the_audit_log(coop, funded):
    cash, bank, _ = _accounts(coop)
    officer = _officer(coop, "sec@imole.coop")

    transfer = _transfer(coop, cash, bank, "1000", officer)

    entry = AuditLog.all_objects.filter(
        cooperative=coop, action="ledger.internal_transfer").first()
    assert entry is not None
    assert entry.actor_id == officer.pk
    assert entry.entity_id == str(transfer.pk)


# ── The guard that keeps this from being a back door ────────────────────────
def test_member_funds_cannot_be_moved_this_way(coop, funded):
    """The reason the asset-only rule exists.

    Debiting 2000 reduces what the cooperative owes its members. If that were
    reachable here it would be a payout with one officer's approval and no
    maker-checker — bypassing the withdrawal flow entirely.
    """
    cash, _, member_funds = _accounts(coop)

    with pytest.raises(TransferError) as exc:
        _transfer(coop, member_funds, cash, "4000")

    assert "asset" in str(exc.value).lower()
    assert member_balance(funded) == Decimal("10000.00")
    assert InternalTransfer.all_objects.count() == 0


def test_money_cannot_be_moved_into_member_funds_either(coop, funded):
    cash, _, member_funds = _accounts(coop)

    with pytest.raises(TransferError):
        _transfer(coop, cash, member_funds, "4000")

    assert InternalTransfer.all_objects.count() == 0


def test_more_than_the_account_holds_is_refused(coop, funded):
    """Otherwise the ledger would show a negative cash balance — money the
    cooperative does not have."""
    cash, bank, _ = _accounts(coop)

    with pytest.raises(TransferError) as exc:
        _transfer(coop, cash, bank, "10000.01")

    assert account_balance(cash) == Decimal("10000.00")
    assert "10,000.00" in str(exc.value) or "available" in str(exc.value).lower()


def test_the_same_account_twice_is_refused(coop, funded):
    cash, _, _ = _accounts(coop)

    with pytest.raises(TransferError) as exc:
        _transfer(coop, cash, cash, "1000")

    assert "different" in str(exc.value).lower()


def test_a_zero_or_negative_amount_is_refused(coop, funded):
    cash, bank, _ = _accounts(coop)

    for bad in ("0", "-500"):
        with pytest.raises(TransferError):
            _transfer(coop, cash, bank, bad)


def test_another_cooperatives_account_is_refused(coop, funded, other_coop):
    """Tenant isolation at the service layer, not only in the queryset."""
    foreign = Account.all_objects.get(cooperative=other_coop, code="1010")
    cash, _, _ = _accounts(coop)

    with pytest.raises(TransferError):
        _transfer(coop, cash, foreign, "1000")

    assert InternalTransfer.all_objects.count() == 0


# ── Who may record one ──────────────────────────────────────────────────────
def test_an_ordinary_member_cannot_record_a_transfer(coop, funded):
    cash, bank, _ = _accounts(coop)
    rank = _officer(coop, "rank@imole.coop", slug=Role.MEMBER)

    resp = _client(rank).post(
        "/api/v1/internal-transfers/",
        {"from_account": cash.pk, "to_account": bank.pk, "amount": "1000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 403
    assert InternalTransfer.all_objects.count() == 0


def test_a_privileged_officer_can_record_one_over_the_api(coop, funded):
    cash, bank, _ = _accounts(coop)
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).post(
        "/api/v1/internal-transfers/",
        {"from_account": cash.pk, "to_account": bank.pk, "amount": "1000",
         "note": "Banked the float"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 201, resp.content
    assert account_balance(bank) == Decimal("1000.00")


def test_a_refused_transfer_answers_400_with_the_reason(coop, funded):
    """TransferError subclasses LedgerError; uncaught it would be a 500 and the
    officer would never learn why."""
    cash, _, member_funds = _accounts(coop)
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).post(
        "/api/v1/internal-transfers/",
        {"from_account": member_funds.pk, "to_account": cash.pk,
         "amount": "1000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 400, resp.content
    assert "asset" in resp.json()["detail"].lower()


def test_an_account_from_another_cooperative_is_not_even_visible(
        coop, funded, other_coop):
    """The queryset is tenant-scoped, so a foreign pk reads as 'not found'
    rather than leaking that it exists."""
    foreign = Account.all_objects.get(cooperative=other_coop, code="1010")
    cash, _, _ = _accounts(coop)
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).post(
        "/api/v1/internal-transfers/",
        {"from_account": cash.pk, "to_account": foreign.pk, "amount": "1000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 400, resp.content
    assert InternalTransfer.all_objects.count() == 0


def test_a_member_may_read_the_transfer_list(coop, funded):
    """Members can see what their cooperative did with its money."""
    rank = _officer(coop, "rank@imole.coop", slug=Role.MEMBER)

    resp = _client(rank).get("/api/v1/internal-transfers/",
                             HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200
