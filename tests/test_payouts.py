"""
The outbound transfer rail.

Every payment the platform sends out goes through ``send_payout`` — a loan
disbursement, a society withdrawing its own wallet, later a savings payout — so
that the wallet check, the ledger posting and the audit trail cannot be
forgotten by one caller.

Two design points these tests pin down:

* **The journal posts when the transfer is initiated, not when it settles.** If
  the wallet were debited only on success, two payouts could each pass the
  balance check and together overdraw the real balance at the provider. A
  later failure reverses the journal instead of preventing it.
* **A provider rejection rolls back completely.** The provider call sits inside
  the transaction, so a refusal leaves no journal and no payout row — nothing
  claiming money moved when it did not.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from audit.models import AuditLog
from core.context import use_tenant
from ledger.models import Account, LedgerEntry
from ledger.services import Line, post_journal
from payments.models import Payout
from payments.providers import PaymentInitError
from payments.services import PayoutError, send_payout, wallet_balance

pytestmark = pytest.mark.django_db

DESTINATION = {
    "destination_bank_name": "Access Bank",
    "destination_bank_code": "044",
    "destination_account_no": "0123456789",
    "account_name": "Adebayo Okonkwo",
}


def _account(coop, code, name, kind):
    with use_tenant(coop):
        account, _ = Account.all_objects.get_or_create(
            cooperative=coop, code=code,
            defaults={"name": name, "kind": kind, "system": True})
    return account


@pytest.fixture
def receivable(coop):
    """The account a loan disbursement debits."""
    return _account(coop, "1200", "Loans Receivable", Account.Kind.ASSET)


@pytest.fixture
def funded_wallet(coop):
    """Put 200,000 in the disbursement wallet, the way a top-up would.

    Posted directly rather than through the top-up flow: this suite is about
    sending money out, and the top-up accounting has its own tests.
    """
    wallet = _account(coop, "1020", "Disbursement Wallet", Account.Kind.ASSET)
    settlement = _account(coop, "1010", "Bank / PSP Settlement",
                          Account.Kind.ASSET)
    with use_tenant(coop):
        post_journal(
            cooperative=coop, reference="TEST-WALLET-FUND",
            memo="Test wallet funding",
            lines=[
                Line(account=wallet, debit=Decimal("200000.00")),
                Line(account=settlement, credit=Decimal("200000.00")),
            ],
        )
    return wallet


def _stub(monkeypatch, *, status="pending", transfer_code="TRF_test01",
          recipient="RCP_test01", raises=None):
    from payments import services

    class _Stub:
        calls: list = []

        def create_transfer_recipient(self, **kwargs):
            _Stub.calls.append(("recipient", kwargs))
            return recipient

        def initiate_transfer(self, **kwargs):
            _Stub.calls.append(("transfer", kwargs))
            if raises:
                raise PaymentInitError(raises)
            return {"status": status, "transfer_code": transfer_code,
                    "raw": {"status": status}}

    _Stub.calls = []
    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())
    return _Stub


def _send(coop, receivable, **overrides):
    kwargs = {
        "cooperative": coop,
        "amount": "50000",
        "debit_account": receivable,
        "kind": Payout.Kind.LOAN,
        "object_id": 1,
        "reason": "Loan 1 disbursement",
        **DESTINATION,
    }
    kwargs.update(overrides)
    with use_tenant(coop):
        return send_payout(**kwargs)


# ── The ledger ──────────────────────────────────────────────────────────────
def test_a_payout_debits_the_target_and_credits_the_wallet(
        coop, receivable, funded_wallet, monkeypatch):
    _stub(monkeypatch)

    payout = _send(coop, receivable)

    lines = {
        e.account.code: e
        for e in LedgerEntry.all_objects.filter(journal=payout.journal)
    }
    assert lines["1200"].debit == Decimal("50000.00")
    assert lines["1020"].credit == Decimal("50000.00")


def test_the_wallet_falls_at_initiation_not_settlement(
        coop, receivable, funded_wallet, monkeypatch):
    """The reason the journal posts on initiation.

    Two payouts that are each affordable against the balance must not be
    affordable together — otherwise they would overdraw the real balance at the
    provider, and the failure would surface as a rejected transfer rather than
    as a refusal here.
    """
    _stub(monkeypatch, status="pending")

    payout = _send(coop, receivable, amount="50000")

    assert payout.status == Payout.Status.PENDING, "not settled yet"
    assert wallet_balance(coop) == Decimal("150000.00"), (
        "the wallet must fall even while the transfer is in flight")


def test_two_payouts_cannot_overdraw_the_wallet_together(
        coop, receivable, funded_wallet, monkeypatch):
    _stub(monkeypatch)

    _send(coop, receivable, amount="150000")

    with pytest.raises(PayoutError) as exc:
        _send(coop, receivable, amount="100000")

    assert "holds" in str(exc.value)
    assert wallet_balance(coop) == Decimal("50000.00")


def test_the_books_stay_balanced(coop, receivable, funded_wallet, monkeypatch):
    from reports.financials import trial_balance

    _stub(monkeypatch)

    _send(coop, receivable)

    assert trial_balance(coop)["balanced"] is True


# ── Guards ──────────────────────────────────────────────────────────────────
def test_more_than_the_wallet_holds_is_refused(coop, receivable, funded_wallet,
                                               monkeypatch):
    _stub(monkeypatch)

    with pytest.raises(PayoutError) as exc:
        _send(coop, receivable, amount="200000.01")

    assert "Fund the wallet" in str(exc.value)
    assert Payout.all_objects.count() == 0


def test_an_empty_wallet_refuses_before_calling_the_provider(
        coop, receivable, monkeypatch):
    stub = _stub(monkeypatch)

    with pytest.raises(PayoutError):
        _send(coop, receivable)

    assert stub.calls == [], "the provider must not be called at all"


def test_a_destination_without_a_bank_code_is_refused(
        coop, receivable, funded_wallet, monkeypatch):
    """Every record written before bank selection is in this state: a transfer
    recipient needs the code, so a bank name alone cannot be paid."""
    _stub(monkeypatch)

    with pytest.raises(PayoutError) as exc:
        _send(coop, receivable, destination_bank_code="")

    assert "bank code" in str(exc.value)
    assert "cash" in str(exc.value).lower(), "it should name the alternative"


def test_a_destination_without_an_account_number_is_refused(
        coop, receivable, funded_wallet, monkeypatch):
    _stub(monkeypatch)

    with pytest.raises(PayoutError):
        _send(coop, receivable, destination_account_no="")


@pytest.mark.parametrize("bad", ["0", "-500"])
def test_a_non_positive_amount_is_refused(coop, receivable, funded_wallet,
                                          monkeypatch, bad):
    _stub(monkeypatch)

    with pytest.raises(PayoutError):
        _send(coop, receivable, amount=bad)


def test_a_junk_amount_is_refused(coop, receivable, funded_wallet,
                                  monkeypatch):
    _stub(monkeypatch)

    with pytest.raises(PayoutError):
        _send(coop, receivable, amount="not-a-number")


def test_another_cooperatives_account_cannot_be_debited(
        coop, other_coop, funded_wallet, monkeypatch):
    """Tenant isolation at the service layer, not only in a queryset."""
    _stub(monkeypatch)
    foreign = _account(other_coop, "1200", "Loans Receivable",
                       Account.Kind.ASSET)

    with pytest.raises(PayoutError) as exc:
        _send(coop, foreign)

    assert "another cooperative" in str(exc.value)


# ── Provider failure rolls everything back ──────────────────────────────────
def test_a_provider_rejection_leaves_no_journal_and_no_payout(
        coop, receivable, funded_wallet, monkeypatch):
    """The provider call sits inside the transaction on purpose: a refusal must
    leave nothing behind claiming money moved."""
    _stub(monkeypatch, raises="Paystack rejected the transfer.")

    with pytest.raises(PaymentInitError):
        _send(coop, receivable)

    assert Payout.all_objects.count() == 0
    assert wallet_balance(coop) == Decimal("200000.00")
    assert not LedgerEntry.all_objects.filter(account__code="1020",
                                              credit__gt=0).exists()


# ── What gets recorded ──────────────────────────────────────────────────────
def test_the_destination_is_snapshotted_onto_the_payout(
        coop, receivable, funded_wallet, monkeypatch):
    """So the record of where money went survives a later edit to the member."""
    _stub(monkeypatch)

    payout = _send(coop, receivable)

    assert payout.destination_bank_name == "Access Bank"
    assert payout.destination_bank_code == "044"
    assert payout.destination_account_no == "0123456789"
    assert payout.destination_account_name == "Adebayo Okonkwo"


def test_the_reference_is_recorded_and_sent_to_the_provider(
        coop, receivable, funded_wallet, monkeypatch):
    """The reference is the provider's idempotency key, so it must be ours."""
    stub = _stub(monkeypatch)

    payout = _send(coop, receivable)

    assert payout.reference.startswith(f"PO-{coop.id}-")
    sent = dict(stub.calls)["transfer"]
    assert sent["reference"] == payout.reference
    assert payout.journal.reference == payout.reference


def test_the_recipient_is_created_from_the_bank_code(
        coop, receivable, funded_wallet, monkeypatch):
    stub = _stub(monkeypatch)

    payout = _send(coop, receivable)

    recipient = dict(stub.calls)["recipient"]
    assert recipient["bank_code"] == "044"
    assert recipient["account_number"] == "0123456789"
    assert payout.recipient_code == "RCP_test01"


def test_a_pending_transfer_is_flagged_for_recheck(
        coop, receivable, funded_wallet, monkeypatch):
    """A webhook can be missed, so a payout can sit pending while the money has
    already left. This flag is what the officer's recheck looks for."""
    _stub(monkeypatch, status="pending")

    payout = _send(coop, receivable)

    assert payout.needs_recheck is True
    assert payout.is_settled is False
    assert payout.settled_at is None


def test_a_provider_success_settles_it(coop, receivable, funded_wallet,
                                       monkeypatch):
    _stub(monkeypatch, status="success")

    payout = _send(coop, receivable)

    assert payout.status == Payout.Status.SUCCESS
    assert payout.is_settled is True
    assert payout.needs_recheck is False
    assert payout.settled_at is not None


def test_a_provider_failure_is_recorded_as_failed(
        coop, receivable, funded_wallet, monkeypatch):
    _stub(monkeypatch, status="failed")

    payout = _send(coop, receivable)

    assert payout.status == Payout.Status.FAILED
    assert payout.failure_reason


def test_it_is_audited(coop, receivable, funded_wallet, monkeypatch, member):
    _stub(monkeypatch)

    payout = _send(coop, receivable, actor=member.user)

    entry = AuditLog.all_objects.filter(action="payment.payout_sent").first()
    assert entry is not None
    assert entry.actor_id == member.user.pk
    assert entry.after["reference"] == payout.reference


def test_describe_names_the_bank_its_code_and_the_account(
        coop, receivable, funded_wallet, monkeypatch):
    _stub(monkeypatch)

    payout = _send(coop, receivable)

    summary = payout.describe()
    assert "Access Bank (044)" in summary
    assert "0123456789" in summary


# ── The dev short-circuit ───────────────────────────────────────────────────
def test_a_simulated_dev_transfer_is_marked_as_such(
        coop, receivable, funded_wallet, settings):
    """With the placeholder key no money moves at all. The payout must say so,
    so a demo payout is never mistaken for a real one."""
    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"

    payout = _send(coop, receivable)

    assert payout.payload.get("simulated") is True
    assert payout.recipient_code == ""
