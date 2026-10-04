"""
What the provider says actually happened.

A transfer being *accepted* is not the same as it settling. Paystack can fail or
reverse one afterwards, and its webhook can be missed or delayed — so a payout
is never trusted once. Two things ask: the ``transfer.*`` webhook, and an
officer's explicit recheck. Both funnel through the same code so they cannot
disagree.

Failure has to undo two things, not one. Reversing the journal returns the money
to the wallet (it was debited when the transfer was initiated, not when it
settled), and the loan has to go back to APPROVED with its schedule deleted —
otherwise a member who received nothing starts accruing arrears.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Account, LedgerEntry
from ledger.services import Line, post_journal
from loans.models import Loan, LoanProduct, RepaymentInstalment
from loans.services import approve_loan
from payments.models import PaymentEvent, Payout
from payments.services import (
    ingest_webhook, recheck_payout, wallet_balance,
)

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def _officer(coop, email="sec@imole.coop", slug=Role.SECRETARY):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name=email, password="x")
        Membership.objects.create(
            user=u, member_no=email[:6],
            role=Role.objects.filter(slug=slug).first())
    return u


def _acct(coop, code, name, kind):
    with use_tenant(coop):
        a, _ = Account.all_objects.get_or_create(
            cooperative=coop, code=code,
            defaults={"name": name, "kind": kind, "system": True})
    return a


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def banked(coop, member):
    member.bank_name = "Access Bank"
    member.bank_code = "044"
    member.bank_account_no = "0123456789"
    member.save(update_fields=["bank_name", "bank_code", "bank_account_no"])
    return member


@pytest.fixture
def funded(coop):
    wallet = _acct(coop, "1020", "Disbursement Wallet", Account.Kind.ASSET)
    settle = _acct(coop, "1010", "Bank / PSP Settlement", Account.Kind.ASSET)
    with use_tenant(coop):
        post_journal(cooperative=coop, reference="TEST-FUND", memo="fund",
                    lines=[Line(account=wallet, debit=Decimal("500000.00")),
                           Line(account=settle, credit=Decimal("500000.00"))])
    return coop


class _Provider:
    """One stub for both directions: it can verify a webhook and move money."""

    fetch_result = None

    def verify(self, raw_body, headers):
        return None

    def create_transfer_recipient(self, **kw):
        return "RCP_x"

    def initiate_transfer(self, **kw):
        return {"status": "pending", "transfer_code": "TRF_x",
                "raw": {"status": "pending"}}

    def fetch_transfer(self, reference):
        return type(self).fetch_result


@pytest.fixture
def provider(monkeypatch):
    from payments import services

    _Provider.fetch_result = None
    monkeypatch.setattr(services, "get_provider", lambda name: _Provider())
    return _Provider


@pytest.fixture
def disbursed(funded, banked, product, provider):
    """A loan approved and paid out, with its transfer still in flight."""
    resp = _client(banked.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "100000", "term_months": 6},
        format="json", **_h(funded))
    loan = Loan.all_objects.get(pk=resp.json()["id"])
    with use_tenant(funded):
        approve_loan(loan, actor=_officer(funded), approve=True)
    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED
    return loan, Payout.all_objects.get()


def _webhook(event, reference, *, status=None, extra=None):
    payload = {"event": event,
               "data": {"reference": reference, "id": 99001,
                        "status": status or event.split(".")[-1],
                        **(extra or {})}}
    return ingest_webhook(provider_name="paystack",
                          raw_body=json.dumps(payload).encode(),
                          headers={})


# ── Success ─────────────────────────────────────────────────────────────────
def test_a_success_webhook_settles_the_payout(disbursed, provider):
    loan, payout = disbursed

    result = _webhook("transfer.success", payout.reference)

    assert result.status == Payout.Status.SUCCESS
    assert result.settled_at is not None
    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED, "already paid; nothing changes"


def test_a_transfer_webhook_is_not_treated_as_a_charge(disbursed, provider):
    """normalize() only recognises charge.success and routes by subaccount, so
    without the dedicated branch every transfer webhook was filed as an ignored
    non-success event."""
    loan, payout = disbursed

    result = _webhook("transfer.success", payout.reference)

    assert isinstance(result, Payout)
    assert not PaymentEvent.all_objects.filter(
        status=PaymentEvent.Status.IGNORED).exists()


def test_success_is_idempotent(disbursed, provider):
    """Paystack resends webhooks."""
    loan, payout = disbursed

    _webhook("transfer.success", payout.reference)
    _webhook("transfer.success", payout.reference)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SUCCESS
    assert payout.reversal_journal_id is None
    assert wallet_balance(payout.cooperative) == Decimal("400000.00")


# ── Failure undoes both sides ───────────────────────────────────────────────
def test_a_failed_transfer_returns_the_money_to_the_wallet(disbursed, provider):
    loan, payout = disbursed
    assert wallet_balance(loan.cooperative) == Decimal("400000.00")

    _webhook("transfer.failed", payout.reference)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    assert payout.reversal_journal_id is not None
    assert wallet_balance(loan.cooperative) == Decimal("500000.00")


def test_a_failed_transfer_sends_the_loan_back_to_approved(disbursed, provider):
    """Otherwise a member who received nothing has a disbursed loan."""
    loan, payout = disbursed

    _webhook("transfer.failed", payout.reference)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED
    assert loan.disbursed_at is None


def test_a_failed_transfer_deletes_the_repayment_schedule(disbursed, provider):
    """The schedule's dates derive from disbursed_at, so keeping it would start
    generating arrears for money never received."""
    loan, payout = disbursed
    assert RepaymentInstalment.all_objects.filter(loan=loan).count() == 6

    _webhook("transfer.failed", payout.reference)

    assert RepaymentInstalment.all_objects.filter(loan=loan).count() == 0


def test_a_reversed_transfer_is_recorded_as_reversed(disbursed, provider):
    loan, payout = disbursed

    _webhook("transfer.reversed", payout.reference)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.REVERSED
    assert wallet_balance(loan.cooperative) == Decimal("500000.00")


def test_the_failure_reason_is_kept(disbursed, provider):
    loan, payout = disbursed

    _webhook("transfer.failed", payout.reference,
             extra={"gateway_response": "Account name mismatch"})

    payout.refresh_from_db()
    assert "Account name mismatch" in payout.failure_reason


def test_the_books_stay_balanced_after_a_reversal(disbursed, provider):
    from reports.financials import trial_balance

    loan, payout = disbursed

    _webhook("transfer.failed", payout.reference)

    assert trial_balance(loan.cooperative)["balanced"] is True


def test_failure_is_idempotent(disbursed, provider):
    """Never reverse twice — that would credit the wallet with money twice."""
    loan, payout = disbursed

    _webhook("transfer.failed", payout.reference)
    _webhook("transfer.failed", payout.reference)

    assert wallet_balance(loan.cooperative) == Decimal("500000.00")
    assert LedgerEntry.all_objects.filter(
        account__code="1020", debit=Decimal("100000.00")).count() == 1


def test_a_late_success_cannot_un_fail_a_reversed_payout(disbursed, provider):
    """Out-of-order webhooks must not resurrect a payout we already undid."""
    loan, payout = disbursed
    _webhook("transfer.failed", payout.reference)

    _webhook("transfer.success", payout.reference)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED


# ── Unknown references are kept, not dropped ────────────────────────────────
def test_a_transfer_for_an_unknown_reference_is_recorded(coop, provider):
    result = _webhook("transfer.success", "PO-nope-nope",
                      extra={"amount": 500000})

    assert isinstance(result, PaymentEvent)
    assert result.status == PaymentEvent.Status.UNMATCHED
    assert "matches no payout" in result.note


def test_an_unknown_transfer_webhook_is_deduplicated(coop, provider):
    _webhook("transfer.success", "PO-nope-nope")
    _webhook("transfer.success", "PO-nope-nope")

    assert PaymentEvent.all_objects.filter(reference="PO-nope-nope").count() == 1


# ── Recheck ─────────────────────────────────────────────────────────────────
def test_recheck_settles_a_payout_the_provider_calls_successful(disbursed,
                                                                provider):
    """The answer to "it still says pending but the money has gone"."""
    loan, payout = disbursed
    provider.fetch_result = {"status": "success", "raw": {"status": "success"}}

    result = recheck_payout(payout)

    assert result.status == Payout.Status.SUCCESS


def test_recheck_reverses_a_payout_the_provider_calls_failed(disbursed,
                                                             provider):
    loan, payout = disbursed
    provider.fetch_result = {"status": "failed", "raw": {"status": "failed"}}

    recheck_payout(payout)

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    assert wallet_balance(loan.cooperative) == Decimal("500000.00")
    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED


def test_recheck_leaves_it_alone_when_the_provider_says_pending(disbursed,
                                                                provider):
    loan, payout = disbursed
    provider.fetch_result = {"status": "pending", "raw": {"status": "pending"}}

    result = recheck_payout(payout)

    assert result.status == Payout.Status.PENDING
    assert result.needs_recheck is True


def test_recheck_is_a_no_op_without_a_live_provider(disbursed, provider):
    """fetch_transfer returns None on the dev key — there is nothing to ask."""
    loan, payout = disbursed
    provider.fetch_result = None

    result = recheck_payout(payout)

    assert result.status == Payout.Status.PENDING


def test_recheck_twice_does_not_double_anything(disbursed, provider):
    loan, payout = disbursed
    provider.fetch_result = {"status": "failed", "raw": {"status": "failed"}}

    recheck_payout(payout)
    recheck_payout(payout)

    assert wallet_balance(loan.cooperative) == Decimal("500000.00")


# ── The endpoint ────────────────────────────────────────────────────────────
def test_an_officer_can_recheck_over_the_api(disbursed, provider):
    loan, payout = disbursed
    provider.fetch_result = {"status": "success", "raw": {"status": "success"}}
    officer = _officer(loan.cooperative, "rechecker@imole.coop")

    resp = _client(officer).post(f"/api/v1/payouts/{payout.id}/recheck/",
                                 **_h(loan.cooperative))

    assert resp.status_code == 200, resp.content
    assert resp.json()["changed"] is True
    assert resp.json()["payout"]["status"] == "success"


def test_recheck_reports_no_change_when_there_is_none(disbursed, provider):
    loan, payout = disbursed
    provider.fetch_result = {"status": "pending", "raw": {}}
    officer = _officer(loan.cooperative, "rechecker@imole.coop")

    resp = _client(officer).post(f"/api/v1/payouts/{payout.id}/recheck/",
                                 **_h(loan.cooperative))

    assert resp.json()["changed"] is False
    assert "No change" in resp.json()["detail"]


def test_a_member_cannot_recheck(disbursed, provider, banked):
    """Rechecking can reverse a journal and send a loan back to approved."""
    loan, payout = disbursed

    resp = _client(banked.user).post(f"/api/v1/payouts/{payout.id}/recheck/",
                                     **_h(loan.cooperative))

    assert resp.status_code == 403


def test_a_member_may_still_read_payouts(disbursed, provider, banked):
    loan, payout = disbursed

    resp = _client(banked.user).get("/api/v1/payouts/", **_h(loan.cooperative))

    assert resp.status_code == 200


def test_unsettled_filters_to_what_is_in_flight(disbursed, provider):
    loan, payout = disbursed
    officer = _officer(loan.cooperative, "o2@imole.coop")

    rows = _client(officer).get("/api/v1/payouts/?unsettled=true",
                                **_h(loan.cooperative)).json()
    results = rows["results"] if isinstance(rows, dict) else rows

    assert [r["id"] for r in results] == [payout.id]


def test_recheck_all_sweeps_everything_in_flight(disbursed, provider):
    loan, payout = disbursed
    provider.fetch_result = {"status": "success", "raw": {"status": "success"}}
    officer = _officer(loan.cooperative, "o3@imole.coop")

    resp = _client(officer).post("/api/v1/payouts/recheck-all/",
                                 **_h(loan.cooperative))

    assert resp.status_code == 200, resp.content
    assert resp.json()["checked"] == 1
    assert resp.json()["changed"] == 1
    payout.refresh_from_db()
    assert payout.status == Payout.Status.SUCCESS
