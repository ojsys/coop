"""
The endpoints behind the awaiting-payment queue.

``pay-electronically`` exists because approval only pays out where it can: a
loan approved before that behaviour existed, or approved while the wallet was
short, sits as APPROVED until someone releases it.

It is deliberately a *separate* action from ``disburse``, which records a cash
handover. For a backlog of old approvals some may already have been settled in
cash outside the system, and one ambiguous "disburse" button is how such a loan
gets paid a second time. The officer has to say which happened.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Account
from ledger.services import Line, post_journal
from loans.models import Loan, LoanProduct
from payments.models import Payout
from payments.services import wallet_balance

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


@pytest.fixture
def stub(monkeypatch):
    from payments import services

    class _Stub:
        def create_transfer_recipient(self, **kw):
            return "RCP_x"

        def initiate_transfer(self, **kw):
            return {"status": "pending", "transfer_code": "TRF_x",
                    "raw": {"status": "pending"}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())


def _approved(coop, member, product):
    """An approved, unpaid loan with its destination snapshotted — the backlog."""
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)
    loan.snapshot_destination()
    loan.save(update_fields=["destination_bank_name", "destination_bank_code",
                             "destination_account_no"])
    return loan


# ── Paying one from the queue ───────────────────────────────────────────────
def test_an_officer_can_release_an_approved_loan(funded, banked, product, stub):
    loan = _approved(funded, banked, product)
    officer = _officer(funded)

    resp = _client(officer).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(funded))

    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "disbursed"
    assert resp.json()["payout_reference"].startswith("PO-")
    assert wallet_balance(funded) == Decimal("400000.00")


def test_it_pays_the_snapshot_not_the_current_account(funded, banked, product,
                                                      stub):
    loan = _approved(funded, banked, product)
    banked.bank_account_no = "9999999999"
    banked.save(update_fields=["bank_account_no"])

    _client(_officer(funded)).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(funded))

    assert Payout.all_objects.get().destination_account_no == "0123456789"


def test_a_short_wallet_answers_400_with_the_reason(coop, banked, product,
                                                    stub):
    loan = _approved(coop, banked, product)

    resp = _client(_officer(coop)).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(coop))

    assert resp.status_code == 400
    assert "Fund the wallet" in resp.json()["detail"]
    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED, "the approval must survive"


def test_no_bank_code_answers_400_and_points_at_cash(funded, member, product,
                                                     stub):
    loan = _approved(funded, member, product)

    resp = _client(_officer(funded)).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(funded))

    assert resp.status_code == 400
    assert "cash" in resp.json()["detail"].lower()


def test_a_provider_refusal_answers_502(funded, banked, product, monkeypatch):
    """Not the officer's input — so it is not a 400, and the message is passed
    through rather than replaced."""
    from payments import services
    from payments.providers import PaymentInitError

    class _Boom:
        def create_transfer_recipient(self, **kw):
            raise PaymentInitError("Paystack rejected the recipient.")

    monkeypatch.setattr(services, "get_provider", lambda n: _Boom())
    loan = _approved(funded, banked, product)

    resp = _client(_officer(funded)).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(funded))

    assert resp.status_code == 502
    assert "Paystack" in resp.json()["detail"]


def test_an_already_disbursed_loan_cannot_be_paid_again(funded, banked,
                                                        product, stub):
    """The guard against paying a loan twice from the queue."""
    loan = _approved(funded, banked, product)
    client = _client(_officer(funded))
    client.post(f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(funded))

    again = client.post(f"/api/v1/loans/{loan.id}/pay-electronically/",
                        **_h(funded))

    assert again.status_code == 400
    assert Payout.all_objects.count() == 1
    assert wallet_balance(funded) == Decimal("400000.00")


def test_a_member_cannot_release_a_payment(funded, banked, product, stub):
    loan = _approved(funded, banked, product)

    resp = _client(banked.user).post(
        f"/api/v1/loans/{loan.id}/pay-electronically/", **_h(funded))

    assert resp.status_code == 403
    assert Payout.all_objects.count() == 0


# ── Recording a cash handover instead ───────────────────────────────────────
def test_recording_cash_does_not_touch_the_wallet(funded, banked, product,
                                                  stub):
    """For a loan already settled in cash outside the system: record it, do not
    pay it again."""
    _acct(funded, "1000", "Cash", Account.Kind.ASSET)
    loan = _approved(funded, banked, product)

    resp = _client(_officer(funded)).post(
        f"/api/v1/loans/{loan.id}/disburse/", **_h(funded))

    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "disbursed"
    assert Payout.all_objects.count() == 0, "no transfer should have been sent"
    assert wallet_balance(funded) == Decimal("500000.00")


# ── The health endpoint ────────────────────────────────────────────────────
def test_the_health_endpoint_separates_the_queue_from_defects(funded, banked,
                                                              product, stub):
    _approved(funded, banked, product)

    body = _client(_officer(funded)).get("/api/v1/loans/health/",
                                        **_h(funded)).json()

    assert body["awaiting_payment"] == 1
    assert body["defects"] == 0, "a backlog is not damage"
    assert body["findings"][0]["code"] == "awaiting_payment"
    assert body["findings"][0]["severity"] == "info"


def test_the_health_endpoint_reports_why_a_loan_is_blocked(coop, banked,
                                                           product, stub):
    _approved(coop, banked, product)

    body = _client(_officer(coop)).get("/api/v1/loans/health/",
                                      **_h(coop)).json()

    assert "wallet holds" in body["findings"][0]["detail"]


def test_a_member_cannot_read_loan_health(funded, banked, product, stub):
    """It names every borrower and amount across the society."""
    _approved(funded, banked, product)

    resp = _client(banked.user).get("/api/v1/loans/health/", **_h(funded))

    assert resp.status_code == 403


def test_health_is_scoped_to_the_active_cooperative(funded, banked, product,
                                                    other_coop, stub):
    _approved(funded, banked, product)
    intruder = _officer(other_coop, email="sec@aba.coop")

    body = _client(intruder).get(
        "/api/v1/loans/health/",
        HTTP_X_COOPERATIVE_ID=str(other_coop.id)).json()

    assert body["checked"] == 0
    assert body["findings"] == []
