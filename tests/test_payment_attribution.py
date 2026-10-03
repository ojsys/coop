"""
Routing a settlement to the right cooperative, and reconstructing past ones.

Attribution used to depend solely on the PSP subaccount code — but split
settlement is off by default, so no subaccount was ever sent, every webhook
resolved to None, and every event was stored with cooperative=NULL. The
reference is the second signal: we issue it ourselves and store it on the record
it belongs to, and the signature is verified before attribution runs.

Deliberately no ``provider_account`` fixture here. test_payments.py always
supplies one, which is precisely why the production failure was invisible to it.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from django.conf import settings
from django.core.management import call_command

from contributions.models import Contribution
from contributions.services import initiate_contribution, record_contribution
from core.context import use_tenant
from payments.models import PaymentEvent, Provider, ProviderAccount
from payments.services import ingest_webhook, reconciliation_summary

pytestmark = pytest.mark.django_db


def _body(*, reference, amount_kobo, event_id, subaccount=None,
          status="success", event="charge.success"):
    data = {
        "id": event_id,
        "reference": reference,
        "amount": amount_kobo,
        "currency": "NGN",
        "status": status,
    }
    if subaccount is not None:
        data["subaccount"] = {"subaccount_code": subaccount}
    return json.dumps({"event": event, "data": data}).encode()


def _sig(body: bytes) -> dict:
    return {
        "x-paystack-signature": hmac.new(
            settings.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512,
        ).hexdigest(),
    }


def _pending(coop, member, dues_type, reference, amount="5000"):
    with use_tenant(coop):
        return initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=amount, psp_reference=reference,
        )


# ── Attribution ─────────────────────────────────────────────────────────────
def test_a_reference_attributes_a_webhook_with_no_subaccount(
        coop, member, dues_type):
    """The production case: split settlement off, so no subaccount is sent."""
    _pending(coop, member, dues_type, "REF-NO-SUB")
    body = _body(reference="REF-NO-SUB", amount_kobo=500000, event_id="w1")

    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))

    assert event.cooperative_id == coop.id
    assert event.status == PaymentEvent.Status.MATCHED
    assert reconciliation_summary(coop)["matched"] == 1


def test_an_unknown_reference_stays_unattributed(coop):
    """Nothing to attribute it to — it must not be guessed at."""
    body = _body(reference="REF-STRANGER", amount_kobo=500000, event_id="w2")

    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))

    assert event.cooperative_id is None
    assert event.status == PaymentEvent.Status.UNMATCHED
    assert "no known subaccount" in event.note


def test_a_subaccount_still_wins_when_present(coop, member, dues_type):
    """Split settlement remains authoritative where it is configured."""
    with use_tenant(coop):
        ProviderAccount.objects.create(provider="paystack",
                                       subaccount_code="ACCT_live_1",
                                       bank_name="GTBank")
    _pending(coop, member, dues_type, "REF-BOTH")
    body = _body(reference="REF-BOTH", amount_kobo=500000, event_id="w3",
                 subaccount="ACCT_live_1")

    event = ingest_webhook(provider_name="paystack", raw_body=body,
                           headers=_sig(body))

    assert event.cooperative_id == coop.id


def test_a_reference_does_not_attribute_across_tenants(
        coop, other_coop, member, dues_type):
    """The reference belongs to one cooperative; it must not leak into another."""
    _pending(coop, member, dues_type, "REF-MINE")
    body = _body(reference="REF-MINE", amount_kobo=500000, event_id="w4")

    ingest_webhook(provider_name="paystack", raw_body=body, headers=_sig(body))

    assert reconciliation_summary(other_coop)["ingested"] == 0


# ── Backfill ────────────────────────────────────────────────────────────────
def _historical(coop, member, dues_type, amount="5000"):
    """A confirmed contribution with no payment event — the pre-fix state."""
    with use_tenant(coop):
        contribution = record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=amount, channel=Contribution.Channel.CASH,
        )
    PaymentEvent.all_objects.filter(
        matched_contribution=contribution).delete()
    return contribution


def test_the_backfill_reconstructs_a_missing_settlement(coop, member,
                                                       dues_type):
    _historical(coop, member, dues_type)
    assert reconciliation_summary(coop)["ingested"] == 0

    call_command("backfill_payment_events")

    assert reconciliation_summary(coop)["matched"] == 1


def test_reconstructed_events_say_so(coop, member, dues_type):
    """They record what the ledger says, not a verified provider settlement —
    the distinction matters to anyone auditing this later."""
    _historical(coop, member, dues_type)

    call_command("backfill_payment_events")

    event = PaymentEvent.all_objects.get()
    assert event.provider == Provider.INTERNAL
    assert event.payload["reconstructed"] is True
    assert "Reconstructed" in event.note


def test_the_backfill_dry_run_writes_nothing(coop, member, dues_type):
    _historical(coop, member, dues_type)

    call_command("backfill_payment_events", "--dry-run")

    assert PaymentEvent.all_objects.count() == 0


def test_the_backfill_is_safe_to_run_twice(coop, member, dues_type):
    _historical(coop, member, dues_type)

    call_command("backfill_payment_events")
    call_command("backfill_payment_events")

    assert PaymentEvent.all_objects.count() == 1


def test_the_backfill_does_not_shadow_a_real_webhook_settlement(
        coop, member, dues_type):
    """A webhook-matched contribution must keep its genuine event rather than
    gain a reconstructed duplicate beside it."""
    _pending(coop, member, dues_type, "REF-REAL")
    body = _body(reference="REF-REAL", amount_kobo=500000, event_id="w5")
    ingest_webhook(provider_name="paystack", raw_body=body, headers=_sig(body))

    call_command("backfill_payment_events")

    events = list(PaymentEvent.all_objects.all())
    assert len(events) == 1
    assert events[0].provider == Provider.PAYSTACK


def test_the_backfill_can_target_one_cooperative(coop, other_coop, member,
                                                 dues_type):
    _historical(coop, member, dues_type)

    call_command("backfill_payment_events", "--cooperative", other_coop.slug)

    assert PaymentEvent.all_objects.count() == 0
