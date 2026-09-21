"""Webhook ingestion + reconciliation engine tests (PRD §6.3, §17)."""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from django.conf import settings

from contributions.models import Contribution
from contributions.services import initiate_contribution
from core.context import use_tenant
from ledger.services import member_balance
from payments.models import PaymentEvent, ProviderAccount
from payments.providers import WebhookVerificationError
from payments.services import ingest_webhook, reconciliation_summary

pytestmark = pytest.mark.django_db

SUBACCOUNT = "ACCT_imole_8821"


@pytest.fixture
def provider_account(coop):
    with use_tenant(coop):
        return ProviderAccount.objects.create(
            provider="paystack", subaccount_code=SUBACCOUNT,
            bank_name="GTBank",
        )


def _paystack_body(*, reference, amount_kobo, event_id,
                   status="success", event="charge.success",
                   subaccount=SUBACCOUNT):
    payload = {
        "event": event,
        "data": {
            "id": event_id,
            "reference": reference,
            "amount": amount_kobo,
            "currency": "NGN",
            "status": status,
            "subaccount": {"subaccount_code": subaccount},
        },
    }
    return json.dumps(payload).encode()


def _sig(body: bytes) -> dict:
    signature = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512,
    ).hexdigest()
    return {"x-paystack-signature": signature}


def _pending(coop, member, dues_type, reference, amount="5000"):
    with use_tenant(coop):
        return initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=amount, psp_reference=reference,
        )


def test_invalid_signature_rejected(coop, provider_account):
    body = _paystack_body(reference="REF1", amount_kobo=500000, event_id="e1")
    with pytest.raises(WebhookVerificationError):
        ingest_webhook(provider_name="paystack", raw_body=body,
                       headers={"x-paystack-signature": "wrong"})
    assert PaymentEvent.all_objects.count() == 0


def test_matched_settlement_confirms_and_posts_ledger(
    coop, member, dues_type, provider_account,
):
    _pending(coop, member, dues_type, "REF-OK", amount="5000")
    body = _paystack_body(reference="REF-OK", amount_kobo=500000,
                          event_id="evt-1")

    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))

    assert event.status == PaymentEvent.Status.MATCHED
    contribution = event.matched_contribution
    contribution.refresh_from_db()
    assert contribution.status == Contribution.Status.CONFIRMED
    assert member_balance(member) == Decimal("5000.00")


def test_idempotent_replay_does_not_double_post(
    coop, member, dues_type, provider_account,
):
    _pending(coop, member, dues_type, "REF-DUP", amount="5000")
    body = _paystack_body(reference="REF-DUP", amount_kobo=500000,
                          event_id="evt-same")

    first = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))
    second = ingest_webhook(provider_name="paystack", raw_body=body,
                            headers=_sig(body))

    assert first.id == second.id           # same record returned
    assert PaymentEvent.all_objects.count() == 1
    assert member_balance(member) == Decimal("5000.00")  # posted once only


def test_duplicate_settlement_flagged(coop, member, dues_type, provider_account):
    _pending(coop, member, dues_type, "REF-2", amount="5000")
    body1 = _paystack_body(reference="REF-2", amount_kobo=500000,
                           event_id="evt-a")
    ingest_webhook(provider_name="paystack", raw_body=body1,
                   headers=_sig(body1))
    # A *different* event id but the contribution is already confirmed.
    body2 = _paystack_body(reference="REF-2", amount_kobo=500000,
                           event_id="evt-b")
    event = ingest_webhook(provider_name="paystack", raw_body=body2,
                           headers=_sig(body2))

    assert event.status == PaymentEvent.Status.DUPLICATE
    assert member_balance(member) == Decimal("5000.00")  # not double-counted


def test_amount_mismatch_flagged_partial(coop, member, dues_type,
                                         provider_account):
    _pending(coop, member, dues_type, "REF-3", amount="5000")
    body = _paystack_body(reference="REF-3", amount_kobo=400000,  # ₦4,000
                          event_id="evt-c")
    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))

    assert event.status == PaymentEvent.Status.PARTIAL
    # Nothing posted for a mismatched amount.
    assert member_balance(member) == Decimal("0.00")


def test_unknown_reference_unmatched(coop, member, dues_type, provider_account):
    body = _paystack_body(reference="NOPE", amount_kobo=500000,
                          event_id="evt-d")
    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))
    assert event.status == PaymentEvent.Status.UNMATCHED


def test_unknown_subaccount_unmatched(coop, member, dues_type):
    body = _paystack_body(reference="X", amount_kobo=500000, event_id="evt-e",
                          subaccount="ACCT_unknown")
    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))
    assert event.status == PaymentEvent.Status.UNMATCHED
    assert event.cooperative_id is None


def test_non_success_event_ignored(coop, member, dues_type, provider_account):
    body = _paystack_body(reference="REF-4", amount_kobo=500000,
                          event_id="evt-f", status="failed",
                          event="charge.failed")
    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))
    assert event.status == PaymentEvent.Status.IGNORED


def test_reconciliation_summary(coop, member, dues_type, provider_account):
    # One matched, one unmatched.
    _pending(coop, member, dues_type, "S-1", amount="5000")
    b1 = _paystack_body(reference="S-1", amount_kobo=500000, event_id="s1")
    ingest_webhook(provider_name="paystack", raw_body=b1, headers=_sig(b1))
    b2 = _paystack_body(reference="S-unknown", amount_kobo=500000,
                        event_id="s2")
    ingest_webhook(provider_name="paystack", raw_body=b2, headers=_sig(b2))

    summary = reconciliation_summary(coop)
    assert summary["ingested"] == 2
    assert summary["matched"] == 1
    assert summary["exceptions"] == 1
    assert summary["match_accuracy"] == 50.0
