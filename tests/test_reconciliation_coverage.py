"""
Reconciliation must show every settlement, not only webhook ones.

The reported symptom was a blank reconciliation screen while the ledger and the
contribution list were both correct. The cause: reconciliation counts
PaymentEvent rows, and three settlement paths created none —

  * verify_payment      (card confirmed on return, no webhook)
  * record_contribution (cash or transfer entered by an officer)
  * anything confirmed before this existed

Why no test caught it: every case in test_payments.py supplies a
``provider_account`` fixture and a payload carrying that subaccount, so the one
condition that fails in production is the one the fixtures guarantee. These
tests deliberately use *no* provider account and *no* webhook.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from contributions.models import Contribution
from contributions.services import initiate_contribution, record_contribution
from core.context import use_tenant
from payments.models import PaymentEvent, Provider
from payments.services import reconciliation_summary, verify_payment

pytestmark = pytest.mark.django_db


def _record(coop, member, dues_type, channel, amount="5000", reference=""):
    with use_tenant(coop):
        return record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=amount, channel=channel, psp_reference=reference,
        )


# ── Cash and transfers ──────────────────────────────────────────────────────
def test_a_cash_contribution_appears_in_reconciliation(coop, member, dues_type):
    _record(coop, member, dues_type, Contribution.Channel.CASH)

    summary = reconciliation_summary(coop)

    assert summary["ingested"] == 1
    assert summary["matched"] == 1
    assert summary["exceptions"] == 0


def test_a_bank_transfer_appears_in_reconciliation(coop, member, dues_type):
    _record(coop, member, dues_type, Contribution.Channel.TRANSFER)

    assert reconciliation_summary(coop)["matched"] == 1


def test_an_internal_settlement_is_not_passed_off_as_a_psp_one(
        coop, member, dues_type):
    """It must be distinguishable from a genuine gateway settlement."""
    _record(coop, member, dues_type, Contribution.Channel.CASH)

    event = PaymentEvent.all_objects.get()
    assert event.provider == Provider.INTERNAL
    assert event.payload["source"] == "internal"
    assert event.payload["reconstructed"] is False


def test_the_reference_falls_back_to_the_journal(coop, member, dues_type):
    """Cash has no psp_reference, so the event must still carry something an
    officer can trace — the ledger journal's own reference."""
    contribution = _record(coop, member, dues_type, Contribution.Channel.CASH)

    event = PaymentEvent.all_objects.get()
    assert event.reference == contribution.journal.reference
    assert event.reference


def test_the_event_is_attributed_to_the_right_cooperative(
        coop, other_coop, member, dues_type):
    """Attribution by construction — no subaccount lookup involved, which is
    what left webhook events orphaned with cooperative=NULL."""
    _record(coop, member, dues_type, Contribution.Channel.CASH)

    assert PaymentEvent.all_objects.get().cooperative_id == coop.id
    assert reconciliation_summary(other_coop)["ingested"] == 0


# ── The browser callback path ───────────────────────────────────────────────
def test_a_callback_settled_payment_appears_in_reconciliation(
        coop, member, dues_type):
    """verify_payment confirmed the contribution and recorded nothing, so a
    card payment with no webhook was invisible to reconciliation."""
    with use_tenant(coop):
        initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", psp_reference="PSTK-CALLBACK-1",
        )

    verify_payment(coop, "PSTK-CALLBACK-1")

    summary = reconciliation_summary(coop)
    assert summary["matched"] == 1
    assert summary["match_accuracy"] == 100.0


def test_a_callback_payment_says_no_webhook_arrived(coop, member, dues_type):
    with use_tenant(coop):
        initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", psp_reference="PSTK-CALLBACK-2",
        )

    verify_payment(coop, "PSTK-CALLBACK-2")

    assert "no webhook" in PaymentEvent.all_objects.get().note


# ── Idempotency ─────────────────────────────────────────────────────────────
def test_verifying_twice_records_one_settlement(coop, member, dues_type):
    """A returning browser plus a retried callback must not double-count."""
    with use_tenant(coop):
        initiate_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", psp_reference="PSTK-TWICE",
        )

    verify_payment(coop, "PSTK-TWICE")
    verify_payment(coop, "PSTK-TWICE")

    assert PaymentEvent.all_objects.count() == 1


def test_recording_a_settlement_twice_is_harmless(coop, member, dues_type):
    """The deterministic event_id is what makes a re-run safe — the backfill
    command depends on this property."""
    from payments.services import record_internal_settlement

    contribution = _record(coop, member, dues_type, Contribution.Channel.CASH)

    first = PaymentEvent.all_objects.get()
    again = record_internal_settlement(contribution)

    assert again.pk == first.pk
    assert PaymentEvent.all_objects.count() == 1


# ── The summary itself ──────────────────────────────────────────────────────
def test_reconciliation_is_no_longer_blank_for_real_activity(
        coop, member, dues_type):
    """The reported bug, stated as an assertion."""
    assert reconciliation_summary(coop)["ingested"] == 0

    _record(coop, member, dues_type, Contribution.Channel.CASH)
    _record(coop, member, dues_type, Contribution.Channel.TRANSFER,
            amount="2500")

    summary = reconciliation_summary(coop)
    assert summary["ingested"] == 2
    assert summary["matched"] == 2
    assert summary["by_status"][PaymentEvent.Status.MATCHED] == 2


def test_amounts_are_carried_onto_the_event(coop, member, dues_type):
    _record(coop, member, dues_type, Contribution.Channel.CASH,
            amount="7250.50")

    event = PaymentEvent.all_objects.get()
    assert event.amount == Decimal("7250.50")
    assert event.currency == coop.base_currency
