"""
The public invoice-pay link.

Unauthenticated by necessity — an officer opens it from the invoice email with
no session — so the pay token is the authorisation. These tests pin the things
that follow from that: an unknown token tells you nothing, a settled invoice
stops being payable, and verification is idempotent.

Offline by construction: with the `sk_test_dev` placeholder key the provider
skips the network call entirely, so nothing here talks to Paystack.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from platform_admin.billing import ensure_subscription, run_billing_cycle
from platform_admin.models import Invoice, Plan

pytestmark = pytest.mark.django_db

VERIFY_URL = "/api/v1/public/invoice/verify/"


def pay_url(token):
    return f"/api/v1/public/invoice/{token}/"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """The pay link is rate limited; stop one test throttling the next."""
    cache.clear()


@pytest.fixture
def invoice(coop):
    coop.contact_email = "officers@imole.coop"
    coop.save(update_fields=["contact_email"])
    Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                        price_monthly=Decimal("10000"))
    sub = ensure_subscription(coop)
    sub.current_period_end = timezone.localdate() - timedelta(days=1)
    sub.save(update_fields=["current_period_end"])
    run_billing_cycle()
    return Invoice.objects.get()


# ── Reading the invoice ─────────────────────────────────────────────────────
def test_a_valid_token_shows_the_invoice(invoice):
    resp = APIClient().get(pay_url(invoice.pay_token))

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["number"] == invoice.number
    assert body["amount"] == "10000.00"
    assert body["payable"] is True


def test_an_unknown_token_is_refused_without_revealing_anything(invoice):
    resp = APIClient().get(pay_url("not-a-real-token"))

    assert resp.status_code == 404
    # The same answer as any other bad token — otherwise the endpoint becomes
    # an oracle for which invoices exist.
    assert "not valid" in resp.json()["detail"]


def test_the_link_needs_no_session(invoice):
    """An officer follows it from an inbox, signed out."""
    client = APIClient()  # no credentials at all

    assert client.get(pay_url(invoice.pay_token)).status_code == 200


# ── Paying ──────────────────────────────────────────────────────────────────
def test_paying_mints_a_reference_on_the_invoice(invoice):
    resp = APIClient().post(pay_url(invoice.pay_token), {}, format="json")

    assert resp.status_code == 200, resp.content
    invoice.refresh_from_db()
    assert invoice.psp_reference, "verification later needs this reference"
    # No live key in tests, so there is no checkout URL — and no network call.
    assert resp.json()["authorization_url"] is None


def test_each_attempt_gets_a_fresh_reference(invoice):
    """Reusing one reference earns Paystack's duplicate-reference error, which
    would leave an officer unable to retry after a browser mishap."""
    client = APIClient()
    client.post(pay_url(invoice.pay_token), {}, format="json")
    invoice.refresh_from_db()
    first = invoice.psp_reference

    client.post(pay_url(invoice.pay_token), {}, format="json")

    invoice.refresh_from_db()
    assert invoice.psp_reference != first


def test_a_settled_invoice_is_no_longer_payable(invoice):
    from platform_admin.billing import settle_invoice

    settle_invoice(invoice)

    resp = APIClient().post(pay_url(invoice.pay_token), {}, format="json")

    assert resp.status_code == 409
    assert "already settled" in resp.json()["detail"]


# ── Verifying ───────────────────────────────────────────────────────────────
def test_verify_settles_the_invoice(invoice):
    APIClient().post(pay_url(invoice.pay_token), {}, format="json")
    invoice.refresh_from_db()

    resp = APIClient().post(VERIFY_URL,
                            {"reference": invoice.psp_reference},
                            format="json")

    assert resp.status_code == 200, resp.content
    assert resp.json()["paid"] is True
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID


def test_verify_is_idempotent(invoice):
    """A returning browser and a retried webhook must not both roll the period."""
    APIClient().post(pay_url(invoice.pay_token), {}, format="json")
    invoice.refresh_from_db()
    ref = invoice.psp_reference
    APIClient().post(VERIFY_URL, {"reference": ref}, format="json")
    invoice.refresh_from_db()
    period_end = invoice.subscription.current_period_end

    APIClient().post(VERIFY_URL, {"reference": ref}, format="json")

    invoice.subscription.refresh_from_db()
    assert invoice.subscription.current_period_end == period_end


def test_verify_rejects_an_unmatched_reference(invoice):
    resp = APIClient().post(VERIFY_URL, {"reference": "SUB-9999-DEADBEEF"},
                            format="json")

    assert resp.status_code == 404


def test_verify_requires_a_reference(invoice):
    resp = APIClient().post(VERIFY_URL, {}, format="json")

    assert resp.status_code == 400


def test_verify_route_is_not_captured_by_the_token_pattern(invoice):
    """`<str:pay_token>` would swallow the literal "verify" if it were declared
    first, making this endpoint unreachable."""
    resp = APIClient().post(VERIFY_URL, {}, format="json")

    # 400 (the verify view complaining about a missing reference), not 404
    # (the pay view failing to find an invoice named "verify").
    assert resp.status_code == 400


def test_the_pay_response_carries_the_reference(invoice):
    """Paystack Inline needs it as `ref`. Without it the on-page modal cannot
    be opened at all, and the only route to payment is the redirect URL — which
    is absent whenever no live key is configured."""
    resp = APIClient().post(pay_url(invoice.pay_token), {}, format="json")

    assert resp.status_code == 200, resp.content
    invoice.refresh_from_db()
    assert resp.json()["reference"] == invoice.psp_reference


# ── Settling without anyone marking it paid ─────────────────────────────────
def _invoice_webhook(reference, amount, event_id="evt-sub-1", subaccount=None):
    import hashlib
    import hmac
    import json

    from django.conf import settings

    from payments.services import ingest_webhook

    data = {"id": event_id, "reference": reference,
            "amount": int(Decimal(amount) * 100), "currency": "NGN",
            "status": "success"}
    if subaccount:
        data["subaccount"] = {"subaccount_code": subaccount}
    body = json.dumps({"event": "charge.success", "data": data}).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), body,
                   hashlib.sha512).hexdigest()
    return ingest_webhook(provider_name="paystack", raw_body=body,
                          headers={"x-paystack-signature": sig})


def _checkout(invoice):
    APIClient().post(pay_url(invoice.pay_token), {}, format="json")
    invoice.refresh_from_db()
    return invoice.psp_reference


def test_the_webhook_settles_the_invoice_and_reopens_the_console(invoice):
    from payments.models import PaymentEvent
    from platform_admin.billing import is_lapsed
    from platform_admin.models import Subscription

    sub = invoice.subscription
    sub.status = Subscription.Status.PAST_DUE
    sub.save(update_fields=["status"])
    end = sub.current_period_end

    event = _invoice_webhook(_checkout(invoice), "10000")

    invoice.refresh_from_db()
    sub.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert sub.status == Subscription.Status.ACTIVE
    assert not is_lapsed(sub)
    assert sub.current_period_end > end
    assert event.status == PaymentEvent.Status.MATCHED
    # Platform billing stays out of the society's own reconciliation.
    assert event.cooperative_id is None


def test_a_replayed_webhook_does_not_roll_the_period_twice(invoice):
    reference = _checkout(invoice)
    _invoice_webhook(reference, "10000")
    invoice.subscription.refresh_from_db()
    end = invoice.subscription.current_period_end

    _invoice_webhook(reference, "10000", event_id="evt-sub-2")
    APIClient().post(VERIFY_URL, {"reference": reference}, format="json")

    invoice.subscription.refresh_from_db()
    assert invoice.subscription.current_period_end == end


def test_a_short_charge_is_left_for_the_platform_team(invoice):
    from payments.models import PaymentEvent

    event = _invoice_webhook(_checkout(invoice), "1")

    invoice.refresh_from_db()
    assert invoice.status != Invoice.Status.PAID
    assert event.status == PaymentEvent.Status.PARTIAL


def test_a_charge_to_a_society_subaccount_does_not_settle(invoice):
    _invoice_webhook(_checkout(invoice), "10000", subaccount="ACCT_x")
    invoice.refresh_from_db()
    assert invoice.status != Invoice.Status.PAID


def test_verify_refuses_a_short_charge(invoice, monkeypatch):
    from payments.providers import PaystackProvider

    monkeypatch.setattr(
        PaystackProvider, "fetch_transaction",
        lambda self, ref: {"success": True, "status": "success",
                           "amount": Decimal("1"), "fee": Decimal("0"),
                           "subaccount": False})
    reference = _checkout(invoice)

    resp = APIClient().post(VERIFY_URL, {"reference": reference},
                            format="json")

    assert resp.json()["paid"] is False


def test_reopening_the_link_finds_a_payment_whose_return_was_lost(
        invoice, monkeypatch, settings):
    """Paid, closed the browser, no webhook: the link itself recovers it."""
    from payments.providers import PaystackProvider

    reference = _checkout(invoice)
    settings.PAYSTACK_SECRET_KEY = "sk_test_real_looking"
    monkeypatch.setattr(
        PaystackProvider, "fetch_transaction",
        lambda self, ref: {"success": ref == reference, "status": "success",
                           "amount": Decimal("10000"), "fee": Decimal("0"),
                           "subaccount": False})

    body = APIClient().get(pay_url(invoice.pay_token)).json()

    assert body["payable"] is False
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID


def test_the_daily_cycle_settles_a_lost_payment_before_chasing_it(
        invoice, monkeypatch, settings):
    from payments.providers import PaystackProvider

    reference = _checkout(invoice)
    settings.PAYSTACK_SECRET_KEY = "sk_test_real_looking"
    monkeypatch.setattr(
        PaystackProvider, "fetch_transaction",
        lambda self, ref: {"success": True, "status": "success",
                           "amount": Decimal("10000"), "fee": Decimal("0"),
                           "subaccount": False})

    report = run_billing_cycle(
        today=timezone.localdate() + timedelta(days=60))

    assert report["rechecked"] == [invoice.number]
    assert report["suspended"] == []


def test_the_return_address_is_this_pay_page(invoice, monkeypatch):
    from payments.providers import PaystackProvider

    seen = {}

    def fake_init(self, **kwargs):
        seen.update(kwargs)
        return "https://checkout.example/x"

    monkeypatch.setattr(PaystackProvider, "initialize_transaction", fake_init)
    APIClient().post(pay_url(invoice.pay_token),
                     {"callback_url": "https://evil.example/"}, format="json")

    assert seen["callback_url"].endswith(f"/billing/pay/{invoice.pay_token}")
    assert "evil" not in seen["callback_url"]


def test_the_platform_team_can_ask_paystack_before_marking_paid(
        invoice, monkeypatch, settings):
    from accounts.models import User
    from payments.providers import PaystackProvider

    admin = User.objects.create_user(email="ops@platform.test",
                                     full_name="Ops", password="x",
                                     is_platform_admin=True)
    _checkout(invoice)
    settings.PAYSTACK_SECRET_KEY = "sk_test_real_looking"
    monkeypatch.setattr(
        PaystackProvider, "fetch_transaction",
        lambda self, ref: {"success": True, "status": "success",
                           "amount": Decimal("10000"), "fee": Decimal("0"),
                           "subaccount": False})
    client = APIClient()
    client.force_authenticate(admin)

    resp = client.post(f"/api/v1/invoices/{invoice.pk}/recheck/")

    assert resp.status_code == 200, resp.content
    assert resp.json()["paid"] is True
    assert resp.json()["status"] == "paid"
