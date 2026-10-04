"""
A society taking its wallet float back.

The wallet only holds money a society deliberately deposited in order to lend,
and that money is its own. Without a way out the platform would be a one-way
door, and the public wording about holding only a withdrawable float would not
be true — so this is as much a correctness requirement as a feature.

One privileged officer rather than maker-checker, deliberately: the only
destination is the society's *own* collection account, which already took two
officers to set. A single officer can move the society's money to the society's
own bank and nowhere else — the dual control is on the destination. It also
matches the rule used throughout: approval is for money leaving the cooperative,
and this is money coming back.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Account, LedgerEntry
from ledger.services import Line, post_journal
from payments.models import Payout
from payments.services import PayoutError, wallet_balance, withdraw_wallet

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _officer(coop, email="sec@imole.coop", slug=Role.SECRETARY):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email,
                                        password="x")
        Membership.objects.create(
            user=user, member_no=email[:6],
            role=Role.objects.filter(slug=slug).first())
    return user


def _account(coop, code, name, kind):
    with use_tenant(coop):
        account, _ = Account.all_objects.get_or_create(
            cooperative=coop, code=code,
            defaults={"name": name, "kind": kind, "system": True})
    return account


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


@pytest.fixture
def banked(coop):
    """A society whose own collection account is on file, with its bank code."""
    coop.bank_name = "Access Bank"
    coop.bank_code = "044"
    coop.bank_account_name = "Imole Cooperative Society"
    coop.bank_account_no = "0123456789"
    coop.save(update_fields=["bank_name", "bank_code", "bank_account_name",
                             "bank_account_no"])
    return coop


@pytest.fixture
def funded(banked):
    wallet = _account(banked, "1020", "Disbursement Wallet",
                      Account.Kind.ASSET)
    settlement = _account(banked, "1010", "Bank / PSP Settlement",
                          Account.Kind.ASSET)
    with use_tenant(banked):
        post_journal(
            cooperative=banked, reference="TEST-FUND",
            memo="Test wallet funding",
            lines=[Line(account=wallet, debit=Decimal("200000.00")),
                   Line(account=settlement, credit=Decimal("200000.00"))],
        )
    return banked


@pytest.fixture
def stub(monkeypatch):
    from payments import services

    class _Stub:
        def create_transfer_recipient(self, **kwargs):
            _Stub.recipient_kwargs = kwargs
            return "RCP_coop"

        def initiate_transfer(self, **kwargs):
            return {"status": "pending", "transfer_code": "TRF_coop",
                    "raw": {"status": "pending"}}

    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())
    return _Stub


# ── It goes to the society's own account ────────────────────────────────────
def test_it_pays_the_societys_own_collection_account(funded, stub):
    with use_tenant(funded):
        payout = withdraw_wallet(funded, amount="50000")

    assert payout.destination_bank_code == "044"
    assert payout.destination_account_no == "0123456789"
    assert payout.destination_account_name == "Imole Cooperative Society"
    assert payout.kind == Payout.Kind.WALLET_WITHDRAWAL


def test_the_recipient_is_built_from_the_societys_details(funded, stub):
    with use_tenant(funded):
        withdraw_wallet(funded, amount="50000")

    assert stub.recipient_kwargs["bank_code"] == "044"
    assert stub.recipient_kwargs["account_number"] == "0123456789"


def test_it_debits_settlement_and_credits_the_wallet(funded, stub):
    """The money comes home: out of the wallet, into the society's own bank."""
    with use_tenant(funded):
        payout = withdraw_wallet(funded, amount="50000")

    lines = {
        e.account.code: e
        for e in LedgerEntry.all_objects.filter(journal=payout.journal)
    }
    assert lines["1010"].debit == Decimal("50000.00")
    assert lines["1020"].credit == Decimal("50000.00")


def test_the_wallet_balance_falls(funded, stub):
    with use_tenant(funded):
        withdraw_wallet(funded, amount="50000")

    assert wallet_balance(funded) == Decimal("150000.00")


def test_the_books_stay_balanced(funded, stub):
    from reports.financials import trial_balance

    with use_tenant(funded):
        withdraw_wallet(funded, amount="50000")

    assert trial_balance(funded)["balanced"] is True


# ── Guards ──────────────────────────────────────────────────────────────────
def test_a_society_with_no_bank_code_cannot_withdraw(coop, stub):
    """Every society whose details predate bank selection is in this state."""
    wallet = _account(coop, "1020", "Disbursement Wallet", Account.Kind.ASSET)
    settlement = _account(coop, "1010", "Bank / PSP Settlement",
                          Account.Kind.ASSET)
    with use_tenant(coop):
        post_journal(cooperative=coop, reference="F", memo="f",
                    lines=[Line(account=wallet, debit=Decimal("1000")),
                           Line(account=settlement, credit=Decimal("1000"))])

        with pytest.raises(PayoutError) as exc:
            withdraw_wallet(coop, amount="500")

    assert "nowhere to withdraw to" in str(exc.value)
    assert "Collection account" in str(exc.value), "it must say where to fix it"
    assert Payout.all_objects.count() == 0


def test_more_than_the_wallet_holds_is_refused(funded, stub):
    with use_tenant(funded):
        with pytest.raises(PayoutError) as exc:
            withdraw_wallet(funded, amount="200000.01")

    assert "holds" in str(exc.value)
    assert wallet_balance(funded) == Decimal("200000.00")


def test_a_non_positive_amount_is_refused(funded, stub):
    with use_tenant(funded):
        for bad in ("0", "-100"):
            with pytest.raises(PayoutError):
                withdraw_wallet(funded, amount=bad)


# ── The endpoint ────────────────────────────────────────────────────────────
def test_an_officer_can_withdraw_over_the_api(funded, stub):
    officer = _officer(funded)

    resp = _client(officer).post("/api/v1/wallet/withdraw/",
                                {"amount": "50000"}, format="json",
                                **_h(funded))

    assert resp.status_code == 201, resp.content
    assert resp.json()["balance"] == "150000.00"
    assert resp.json()["payout"]["destination_account_no"] == "0123456789"


def test_an_ordinary_member_cannot_withdraw(funded, member, stub):
    resp = _client(member.user).post("/api/v1/wallet/withdraw/",
                                     {"amount": "50000"}, format="json",
                                     **_h(funded))

    assert resp.status_code == 403
    assert Payout.all_objects.count() == 0


def test_an_incomplete_account_answers_400_with_the_reason(coop, stub):
    officer = _officer(coop)

    resp = _client(officer).post("/api/v1/wallet/withdraw/",
                                 {"amount": "500"}, format="json", **_h(coop))

    assert resp.status_code == 400
    assert "nowhere to withdraw to" in resp.json()["detail"]


def test_a_short_wallet_answers_400(funded, stub):
    officer = _officer(funded)

    resp = _client(officer).post("/api/v1/wallet/withdraw/",
                                 {"amount": "999999"}, format="json",
                                 **_h(funded))

    assert resp.status_code == 400
    assert "holds" in resp.json()["detail"]
