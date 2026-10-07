"""
The wallet endpoints.

``tests/test_wallet.py`` covers the accounting; this covers the API around it —
in particular who may *fund* the wallet, which moves real money out of the
society's bank account and into the platform's balance.

Reads are deliberately open to any office-holder: a Chairperson needs to see
whether there is anything to lend from, and SettingsPage states the principle
("writable only by a privileged member, but do not render a card that will only
ever 403"). Funding is privileged.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from payments.models import WalletTopUp

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


def _stub(monkeypatch, *, success=True, amount="50000.00", fee="750.00",
          url="https://paystack.test/checkout", init_raises=None):
    from payments import services
    from payments.providers import PaymentInitError

    class _Stub:
        def initialize_transaction(self, **kwargs):
            if init_raises:
                raise PaymentInitError(init_raises)
            return url

        def fetch_transaction(self, reference):
            return {"success": success, "amount": Decimal(amount),
                    "fee": Decimal(fee)}

    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


# ── Reading the balance ─────────────────────────────────────────────────────
def test_the_balance_starts_at_zero(coop, member):
    body = _client(member.user).get("/api/v1/wallet/", **_h(coop)).json()

    assert body["balance"] == "0.00"
    assert body["topups"] == []
    assert body["currency"] == coop.base_currency


def test_any_office_holder_may_read_the_balance(coop, member):
    """Not closed: a Chairperson must be able to see whether lending is
    possible, and a card that only ever 403s should not be rendered at all."""
    resp = _client(member.user).get("/api/v1/wallet/", **_h(coop))

    assert resp.status_code == 200


def test_the_balance_reflects_a_confirmed_topup(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)
    client = _client(officer)

    created = client.post("/api/v1/wallet/topup/", {"amount": "50000"},
                          format="json", **_h(coop))
    reference = created.json()["topup"]["psp_reference"]
    client.post("/api/v1/wallet/verify/", {"reference": reference},
                format="json", **_h(coop))

    body = client.get("/api/v1/wallet/", **_h(coop)).json()
    assert body["balance"] == "49250.00"
    assert len(body["topups"]) == 1
    assert body["topups"][0]["is_confirmed"] is True


# ── Funding it ──────────────────────────────────────────────────────────────
def test_an_officer_can_start_a_topup(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)

    resp = _client(officer).post("/api/v1/wallet/topup/", {"amount": "50000"},
                                 format="json", **_h(coop))

    assert resp.status_code == 201, resp.content
    assert resp.json()["authorization_url"] == "https://paystack.test/checkout"
    assert resp.json()["topup"]["status"] == "pending"


def test_starting_a_topup_credits_nothing(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)
    client = _client(officer)

    client.post("/api/v1/wallet/topup/", {"amount": "50000"}, format="json",
                **_h(coop))

    assert client.get("/api/v1/wallet/", **_h(coop)).json()["balance"] == "0.00"


def test_an_ordinary_member_cannot_fund_the_wallet(coop, member, monkeypatch):
    """Funding moves money out of the society's bank account."""
    _stub(monkeypatch)

    resp = _client(member.user).post(
        "/api/v1/wallet/topup/", {"amount": "50000"}, format="json", **_h(coop))

    assert resp.status_code == 403
    assert WalletTopUp.all_objects.count() == 0


def test_a_bad_amount_answers_400_with_the_reason(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)

    resp = _client(officer).post("/api/v1/wallet/topup/", {"amount": "-5"},
                                 format="json", **_h(coop))

    assert resp.status_code == 400
    assert "positive" in resp.json()["detail"]


def test_a_provider_refusal_answers_502_verbatim(coop, monkeypatch):
    """"Paystack rejected the transaction" is actionable; a generic failure is
    not — and it is not the officer's input that was wrong."""
    _stub(monkeypatch, init_raises="Paystack rejected the transaction.")
    officer = _officer(coop)

    resp = _client(officer).post("/api/v1/wallet/topup/", {"amount": "50000"},
                                 format="json", **_h(coop))

    assert resp.status_code == 502
    assert "Paystack" in resp.json()["detail"]


# ── Confirming ──────────────────────────────────────────────────────────────
def test_verify_credits_the_wallet_and_returns_the_balance(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)
    client = _client(officer)
    created = client.post("/api/v1/wallet/topup/", {"amount": "50000"},
                          format="json", **_h(coop))
    reference = created.json()["topup"]["psp_reference"]

    resp = client.post("/api/v1/wallet/verify/", {"reference": reference},
                       format="json", **_h(coop))

    assert resp.status_code == 200, resp.content
    assert resp.json()["balance"] == "49250.00"
    assert resp.json()["topup"]["net_amount"] == "49250.00"


def test_verify_is_idempotent_over_the_api(coop, monkeypatch):
    """The payment return URL can be replayed by a browser refresh."""
    _stub(monkeypatch)
    officer = _officer(coop)
    client = _client(officer)
    reference = client.post(
        "/api/v1/wallet/topup/", {"amount": "50000"}, format="json",
        **_h(coop)).json()["topup"]["psp_reference"]

    client.post("/api/v1/wallet/verify/", {"reference": reference},
                format="json", **_h(coop))
    second = client.post("/api/v1/wallet/verify/", {"reference": reference},
                         format="json", **_h(coop))

    assert second.json()["balance"] == "49250.00", "credited once, not twice"


def test_verify_without_a_reference_answers_400(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)

    resp = _client(officer).post("/api/v1/wallet/verify/", {}, format="json",
                                 **_h(coop))

    assert resp.status_code == 400
    assert "reference" in resp.json()["detail"].lower()


def test_verify_with_an_unknown_reference_answers_404(coop, monkeypatch):
    _stub(monkeypatch)
    officer = _officer(coop)

    resp = _client(officer).post("/api/v1/wallet/verify/",
                                 {"reference": "WLT-nope"}, format="json",
                                 **_h(coop))

    assert resp.status_code == 404


def test_a_member_cannot_confirm_a_topup(coop, member, monkeypatch):
    _stub(monkeypatch)

    resp = _client(member.user).post(
        "/api/v1/wallet/verify/", {"reference": "WLT-anything"},
        format="json", **_h(coop))

    assert resp.status_code == 403


# ── Tenant isolation ────────────────────────────────────────────────────────
def test_one_society_cannot_confirm_anothers_topup(coop, other_coop,
                                                   monkeypatch):
    """The reference is looked up within the active cooperative, so a leaked
    reference cannot credit the wrong wallet."""
    _stub(monkeypatch)
    officer = _officer(coop)
    reference = _client(officer).post(
        "/api/v1/wallet/topup/", {"amount": "50000"}, format="json",
        **_h(coop)).json()["topup"]["psp_reference"]

    intruder = _officer(other_coop, email="sec@aba.coop")
    resp = _client(intruder).post(
        "/api/v1/wallet/verify/", {"reference": reference}, format="json",
        HTTP_X_COOPERATIVE_ID=str(other_coop.id))

    assert resp.status_code == 404
    assert WalletTopUp.all_objects.get(
        psp_reference=reference).status == WalletTopUp.Status.PENDING


# ── Funding history vs. attempts ────────────────────────────────────────────
def test_unpaid_attempts_are_kept_apart_from_real_funding(coop, monkeypatch):
    """Every click on "Fund wallet" creates a row before any money moves; the
    funding history must show only what actually arrived."""
    _stub(monkeypatch)
    client = _client(_officer(coop))

    paid = client.post("/api/v1/wallet/topup/", {"amount": "50000"},
                       format="json", **_h(coop)).json()["topup"]
    client.post("/api/v1/wallet/verify/", {"reference": paid["psp_reference"]},
                format="json", **_h(coop))
    for _ in range(3):
        client.post("/api/v1/wallet/topup/", {"amount": "50000"},
                    format="json", **_h(coop))
    with use_tenant(coop):
        WalletTopUp.objects.create(amount=Decimal("10"), psp_reference="WLT-F",
                                   status=WalletTopUp.Status.FAILED)

    body = client.get("/api/v1/wallet/", **_h(coop)).json()

    assert [t["psp_reference"] for t in body["topups"]] == [
        paid["psp_reference"]]
    assert all(t["status"] != "confirmed" for t in body["attempts"])
    assert len(body["attempts"]) == 4
    assert body["attempt_counts"] == {"pending": 3, "failed": 1}
