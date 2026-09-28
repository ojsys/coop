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
