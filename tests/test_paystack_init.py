"""Paystack transaction initialization (outbound checkout creation)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.conf import settings

from contributions.services import initiate_contribution
from core.context import use_tenant
from payments.models import ProviderAccount
from contributions.models import Contribution
from ledger.services import member_balance
from payments.providers import PaymentInitError, get_provider
from payments.services import initialize_payment, verify_payment

pytestmark = pytest.mark.django_db

SUBACCOUNT = "ACCT_imole_8821"


@pytest.fixture
def paystack_account(coop):
    with use_tenant(coop):
        return ProviderAccount.objects.create(
            provider="paystack", subaccount_code=SUBACCOUNT, bank_name="GTBank",
        )


@pytest.fixture
def pending(coop, member, dues_type):
    with use_tenant(coop):
        return initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", channel="psp", psp_reference="OFFLINE-abc123",
        )


def test_dev_key_skips_live_call(pending, paystack_account, settings):
    """With the dev placeholder key, no network call is made and URL is None."""
    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"
    url = initialize_payment(pending, email="ada@example.com")
    assert url is None


def test_initialize_builds_correct_paystack_request(
    pending, paystack_account, settings, monkeypatch,
):
    """A live key produces a kobo amount, the coop subaccount, and our reference."""
    settings.PAYSTACK_SECRET_KEY = "sk_test_LIVEKEY"
    settings.PAYSTACK_USE_SUBACCOUNT = True  # opt in to split settlement
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": True,
                    "data": {"authorization_url": "https://paystack.test/xyz"}}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr("payments.providers.requests.post", fake_post)

    url = initialize_payment(pending, email="ada@example.com")

    assert url == "https://paystack.test/xyz"
    assert captured["url"].endswith("/transaction/initialize")
    assert captured["headers"]["Authorization"] == "Bearer sk_test_LIVEKEY"
    body = captured["json"]
    assert body["amount"] == 500000            # ₦5,000 → kobo
    assert body["reference"] == "OFFLINE-abc123"  # reused for webhook reconciliation
    assert body["subaccount"] == SUBACCOUNT     # settles to the coop's own account
    assert body["email"] == "ada@example.com"


def test_provider_error_raises_payment_init_error(
    pending, paystack_account, settings, monkeypatch,
):
    settings.PAYSTACK_SECRET_KEY = "sk_test_LIVEKEY"

    def boom(*a, **k):
        import requests
        raise requests.RequestException("network down")

    monkeypatch.setattr("payments.providers.requests.post", boom)
    with pytest.raises(PaymentInitError):
        initialize_payment(pending, email="ada@example.com")


def test_initiate_endpoint_returns_authorization_url(
    coop, member, dues_type, paystack_account,
):
    """The initiate API returns an authorization_url field (None in dev)."""
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(member.user)
    resp = client.post(
        "/api/v1/contributions/initiate/",
        {"membership": member.id, "contribution_type": dues_type.id,
         "amount": "5000", "channel": "psp", "psp_reference": "OFFLINE-xyz"},
        format="json",
        HTTP_X_COOPERATIVE_ID=str(coop.id),
    )
    assert resp.status_code == 201
    assert "authorization_url" in resp.data
    assert resp.data["status"] == "pending"


def test_verify_confirms_and_posts_to_ledger(pending, coop, member, settings):
    """Dev mode: verifying a pending payment confirms it and moves the balance."""
    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"
    with use_tenant(coop):
        assert member_balance(member) == Decimal("0.00")
        confirmed = verify_payment(coop, "OFFLINE-abc123")
        assert confirmed.status == Contribution.Status.CONFIRMED
        assert member_balance(member) == Decimal("5000.00")


def test_verify_is_idempotent(pending, coop, member, settings):
    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"
    with use_tenant(coop):
        verify_payment(coop, "OFFLINE-abc123")
        verify_payment(coop, "OFFLINE-abc123")  # second call: no double-post
        assert member_balance(member) == Decimal("5000.00")


def test_verify_failed_charge_stays_pending(pending, coop, settings, monkeypatch):
    """A real key that returns a non-success charge leaves the contribution pending."""
    settings.PAYSTACK_SECRET_KEY = "sk_test_LIVEKEY"

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": True, "data": {"status": "abandoned"}}

    monkeypatch.setattr("payments.providers.requests.get",
                        lambda *a, **k: FakeResp())
    with use_tenant(coop):
        result = verify_payment(coop, "OFFLINE-abc123")
        assert result.status == Contribution.Status.PENDING


def test_verify_endpoint(coop, member, dues_type, settings):
    from rest_framework.test import APIClient

    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"
    with use_tenant(coop):
        initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", channel="psp", psp_reference="VER-1",
        )
    client = APIClient()
    client.force_authenticate(member.user)
    resp = client.post("/api/v1/contributions/verify/", {"reference": "VER-1"},
                       format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))
    assert resp.status_code == 200
    assert resp.data["status"] == "confirmed"
