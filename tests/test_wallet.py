"""
The disbursement wallet.

Money a society deliberately deposits with the platform so it can lend
electronically. It is the only money the platform holds — collections settle to
each society's own bank through its subaccount — and it is the society's own.

The accounting is the part worth reading. A society pays the gross; the provider
deducts its fee before the money reaches the balance; so the wallet can only be
credited the **net**:

    debit  1020 Disbursement Wallet    net
    debit  5000 Payment Charges        fee
    credit 1010 Bank / PSP Settlement  gross

Crediting the gross would claim money that never arrived, and the failure would
surface much later as a disbursement rejected for insufficient funds at the
provider — far from its cause.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.context import use_tenant
from ledger.models import Account, LedgerEntry
from ledger.services import account_balance
from payments.models import WalletTopUp
from payments.services import (
    WalletError, confirm_wallet_topup, initiate_wallet_topup, wallet_balance,
)
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _stub_provider(monkeypatch, *, success=True, amount=None, fee="0.00"):
    """Stand in for the provider's verify call with a known fee."""
    from payments import services

    class _Stub:
        def initialize_transaction(self, **kwargs):
            _Stub.init_kwargs = kwargs
            return "https://paystack.test/checkout"

        def fetch_transaction(self, reference):
            return {
                "success": success,
                "amount": Decimal(amount) if amount is not None else None,
                "fee": Decimal(fee),
            }

    stub = _Stub()
    monkeypatch.setattr(services, "get_provider", lambda name: stub)
    return _Stub


# ── Initiating ──────────────────────────────────────────────────────────────
def test_initiating_posts_nothing(coop, monkeypatch):
    """No money has moved yet, so no journal — same rule as a contribution."""
    _stub_provider(monkeypatch)

    topup, url = initiate_wallet_topup(
        coop, amount="50000", email="treasurer@imole.coop")

    assert topup.status == WalletTopUp.Status.PENDING
    assert topup.journal is None
    assert url == "https://paystack.test/checkout"
    assert wallet_balance(coop) == Decimal("0.00")


def test_the_checkout_never_carries_a_subaccount(coop, monkeypatch):
    """The load-bearing detail.

    Every other checkout routes settlement to the society's own subaccount.
    Doing that here would send the society's money straight back to its own
    bank instead of the platform balance the wallet represents — funding
    nothing, while the ledger said otherwise.
    """
    stub = _stub_provider(monkeypatch)

    initiate_wallet_topup(coop, amount="50000", email="t@imole.coop")

    assert stub.init_kwargs["subaccount_code"] is None


def test_a_non_positive_amount_is_refused(coop, monkeypatch):
    _stub_provider(monkeypatch)

    for bad in ("0", "-100"):
        with pytest.raises(WalletError):
            initiate_wallet_topup(coop, amount=bad, email="t@imole.coop")

    assert WalletTopUp.all_objects.count() == 0


def test_a_junk_amount_is_refused(coop, monkeypatch):
    _stub_provider(monkeypatch)

    with pytest.raises(WalletError):
        initiate_wallet_topup(coop, amount="not-a-number",
                              email="t@imole.coop")


# ── Confirming ──────────────────────────────────────────────────────────────
def test_confirmation_credits_the_net_not_the_gross(coop, monkeypatch):
    _stub_provider(monkeypatch, amount="50000.00", fee="750.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")

    confirmed = confirm_wallet_topup(coop, topup.psp_reference)

    assert confirmed.status == WalletTopUp.Status.CONFIRMED
    assert confirmed.fee == Decimal("750.00")
    assert confirmed.net_amount == Decimal("49250.00")
    assert wallet_balance(coop) == Decimal("49250.00"), (
        "the wallet must hold only what actually reached the provider")


def test_the_journal_books_all_three_legs(coop, monkeypatch):
    _stub_provider(monkeypatch, amount="50000.00", fee="750.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")

    confirm_wallet_topup(coop, topup.psp_reference)

    topup.refresh_from_db()
    lines = {
        e.account.code: e
        for e in LedgerEntry.all_objects.filter(journal=topup.journal)
    }
    assert lines["1020"].debit == Decimal("49250.00")   # wallet, net
    assert lines["5000"].debit == Decimal("750.00")     # the provider's fee
    assert lines["1010"].credit == Decimal("50000.00")  # the society paid gross


def test_the_books_stay_balanced(coop, monkeypatch):
    _stub_provider(monkeypatch, amount="50000.00", fee="750.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")

    confirm_wallet_topup(coop, topup.psp_reference)

    assert trial_balance(coop)["balanced"] is True


def test_a_zero_fee_books_only_two_legs(coop, monkeypatch):
    """With no live key the provider reports a zero fee. A zero-value expense
    line would be noise in the ledger."""
    _stub_provider(monkeypatch, amount="50000.00", fee="0.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")

    confirm_wallet_topup(coop, topup.psp_reference)

    topup.refresh_from_db()
    codes = {
        e.account.code
        for e in LedgerEntry.all_objects.filter(journal=topup.journal)
    }
    assert codes == {"1020", "1010"}


def test_confirming_twice_credits_the_wallet_once(coop, monkeypatch):
    """A replayed return URL or a retried webhook must not double the balance."""
    _stub_provider(monkeypatch, amount="50000.00", fee="750.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")

    confirm_wallet_topup(coop, topup.psp_reference)
    confirm_wallet_topup(coop, topup.psp_reference)

    assert wallet_balance(coop) == Decimal("49250.00")
    assert LedgerEntry.all_objects.filter(account__code="1020").count() == 1


def test_an_abandoned_charge_leaves_it_pending_with_no_journal(coop,
                                                               monkeypatch):
    _stub_provider(monkeypatch, success=False, amount="50000.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")

    result = confirm_wallet_topup(coop, topup.psp_reference)

    assert result.status == WalletTopUp.Status.PENDING
    assert result.journal is None
    assert wallet_balance(coop) == Decimal("0.00")


def test_an_unknown_reference_returns_none(coop, monkeypatch):
    _stub_provider(monkeypatch)

    assert confirm_wallet_topup(coop, "WLT-does-not-exist") is None


def test_a_fee_larger_than_the_payment_is_refused(coop, monkeypatch):
    """Nonsense from the provider must not post a negative wallet credit."""
    _stub_provider(monkeypatch, amount="100.00", fee="100.00")
    topup, _ = initiate_wallet_topup(coop, amount="100", email="t@imole.coop")

    with pytest.raises(WalletError):
        confirm_wallet_topup(coop, topup.psp_reference)


# ── Accounts are created on first use ───────────────────────────────────────
def test_the_wallet_accounts_appear_for_a_society_that_predates_them(
        coop, monkeypatch):
    """Societies provisioned before 1020/5000 joined the baseline chart must
    not need a backfill — the accounts are get-or-created on first use."""
    with use_tenant(coop):
        Account.all_objects.filter(
            cooperative=coop, code__in=["1020", "5000"]).delete()

    _stub_provider(monkeypatch, amount="50000.00", fee="750.00")
    topup, _ = initiate_wallet_topup(coop, amount="50000",
                                     email="t@imole.coop")
    confirm_wallet_topup(coop, topup.psp_reference)

    assert Account.all_objects.filter(cooperative=coop, code="1020").exists()
    assert Account.all_objects.filter(cooperative=coop, code="5000").exists()
    assert wallet_balance(coop) == Decimal("49250.00")


def test_a_new_society_gets_both_accounts_at_provisioning(coop):
    for code in ("1020", "5000"):
        account = Account.all_objects.get(cooperative=coop, code=code)
        assert account.system is True
    assert account_balance(
        Account.all_objects.get(cooperative=coop, code="1020")
    ) == Decimal("0.00")


# ── Pending top-ups the provider has already been paid for ─────────────────
# A top-up used to be confirmed only by the officer's return from checkout. One
# who paid and closed the tab left it PENDING with the money at the provider.
def _stub_status(monkeypatch, status, *, amount="50000", fee="0.00"):
    from payments import services
    from payments.providers import PaystackProvider

    # The real provider, so a webhook's signature is still verified; only the
    # question "what happened to this charge" is answered here.
    class _Stub(PaystackProvider):
        calls = 0

        def fetch_transaction(self, reference):
            _Stub.calls += 1
            return {"success": status == "success", "status": status,
                    "amount": Decimal(amount), "fee": Decimal(fee)}

    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())
    return _Stub


def _pending_topup(coop, reference="WLT-STUCK-1", amount="50000.00"):
    return WalletTopUp.all_objects.create(
        cooperative=coop, amount=Decimal(amount), psp_reference=reference)


def test_a_recheck_confirms_a_topup_the_provider_was_paid_for(coop,
                                                              monkeypatch):
    from payments.services import recheck_wallet_topup

    _stub_status(monkeypatch, "success", fee="750.00")
    topup = recheck_wallet_topup(_pending_topup(coop))

    assert topup.is_confirmed
    assert topup.net_amount == Decimal("49250.00")
    assert wallet_balance(coop) == Decimal("49250.00")


def test_a_recheck_never_credits_twice(coop, monkeypatch):
    from payments.services import recheck_wallet_topup

    stub = _stub_status(monkeypatch, "success")
    topup = _pending_topup(coop)
    recheck_wallet_topup(topup)
    recheck_wallet_topup(topup)

    assert wallet_balance(coop) == Decimal("50000.00")
    assert stub.calls == 1, "a confirmed top-up must not be re-asked"


def test_an_abandoned_checkout_stays_pending(coop, monkeypatch):
    """Paystack lets a payer resume an abandoned checkout, so it is not final."""
    from payments.services import recheck_wallet_topup

    _stub_status(monkeypatch, "abandoned")
    topup = recheck_wallet_topup(_pending_topup(coop))

    assert topup.status == WalletTopUp.Status.PENDING
    assert wallet_balance(coop) == Decimal("0.00")


def test_a_failed_charge_is_marked_failed(coop, monkeypatch):
    from payments.services import recheck_wallet_topup

    _stub_status(monkeypatch, "failed")
    topup = recheck_wallet_topup(_pending_topup(coop))

    assert topup.status == WalletTopUp.Status.FAILED
    assert topup.journal is None


def test_the_webhook_confirms_a_topup(coop, monkeypatch):
    """Previously filed as unmatched, leaving the top-up pending."""
    import hashlib
    import hmac
    import json

    from django.conf import settings

    from payments.models import PaymentEvent
    from payments.services import ingest_webhook

    _stub_status(monkeypatch, "success", fee="750.00")
    topup = _pending_topup(coop, reference="WLT-HOOK-1")
    body = json.dumps({"event": "charge.success", "data": {
        "id": "hook-1", "reference": "WLT-HOOK-1", "amount": 5000000,
        "currency": "NGN", "status": "success"}}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body,
                   hashlib.sha512).hexdigest()

    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers={"x-paystack-signature": sig})

    topup.refresh_from_db()
    assert event.cooperative_id == coop.id
    assert event.status == PaymentEvent.Status.MATCHED
    assert topup.is_confirmed
    assert wallet_balance(coop) == Decimal("49250.00")


def test_the_command_confirms_stuck_topups(coop, monkeypatch, settings):
    from django.core.management import call_command

    settings.PAYSTACK_SECRET_KEY = "sk_live_real"
    _stub_status(monkeypatch, "success")
    topup = _pending_topup(coop)

    call_command("recheck_pending_payments")

    topup.refresh_from_db()
    assert topup.is_confirmed


def test_the_command_refuses_a_simulated_provider(coop, settings):
    """Without a live key every reference "succeeds" — crediting on that would
    put money in the wallet that was never paid."""
    from django.core.management import call_command

    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"
    topup = _pending_topup(coop)

    call_command("recheck_pending_payments")

    topup.refresh_from_db()
    assert topup.status == WalletTopUp.Status.PENDING
