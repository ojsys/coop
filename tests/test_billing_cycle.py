"""
The subscription lifecycle: free month, invoice, reminders, suspension.

Policy under test — a cooperative goes live, operates free for a month, is then
invoiced, reminded every few days while it owes, and suspended only after a long
grace period. The suspension tests matter most: suspending locks real members
out of their own savings records, so it must not happen early or by accident.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from platform_admin.billing import (ensure_subscription, run_billing_cycle,
                                    settle_invoice)
from platform_admin.models import Invoice, Plan, Subscription
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.fixture
def plan():
    return Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                               price_monthly=Decimal("10000"))


@pytest.fixture(autouse=True)
def _contactable(coop):
    """Invoices need somewhere to go, or nothing is ever sent."""
    coop.contact_email = "officers@imole.coop"
    coop.save(update_fields=["contact_email"])
    return coop


# ── Trial ───────────────────────────────────────────────────────────────────
def test_going_live_starts_a_free_month(coop, plan, settings):
    sub = ensure_subscription(coop)

    assert sub is not None
    assert sub.status == Subscription.Status.TRIAL
    assert sub.current_period_end == (
        sub.current_period_start + timedelta(days=settings.BILLING_TRIAL_DAYS))
    assert Invoice.objects.count() == 0, "the first month is free"


def test_ensure_subscription_is_idempotent(coop, plan):
    first = ensure_subscription(coop)
    assert ensure_subscription(coop).pk == first.pk
    assert Subscription.objects.count() == 1


def test_a_missing_price_list_does_not_block_going_live(coop):
    """No plan for the tier is the platform's problem, not the cooperative's."""
    assert Plan.objects.count() == 0

    assert ensure_subscription(coop) is None
    assert Subscription.objects.count() == 0


# ── Invoicing ───────────────────────────────────────────────────────────────
def _expire_trial(sub, days_ago=0):
    sub.current_period_end = timezone.localdate() - timedelta(days=days_ago)
    sub.save(update_fields=["current_period_end"])
    return sub


def test_the_first_invoice_is_raised_when_the_free_month_ends(coop, plan):
    sub = _expire_trial(ensure_subscription(coop))
    mail.outbox.clear()

    report = run_billing_cycle()

    invoice = Invoice.objects.get()
    assert invoice.amount == Decimal("10000")
    assert invoice.status == Invoice.Status.SENT
    assert invoice.pay_token, "the email needs a pay link"
    assert len(report["issued"]) == 1
    assert len(mail.outbox) == 1


def test_a_second_pass_the_same_day_does_not_double_invoice(coop, plan):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()

    run_billing_cycle()

    assert Invoice.objects.count() == 1, "cron must be safe to run twice"


def test_dry_run_changes_nothing(coop, plan):
    _expire_trial(ensure_subscription(coop))
    mail.outbox.clear()

    report = run_billing_cycle(dry_run=True)

    assert report["issued"], "it should still say what it would do"
    assert Invoice.objects.count() == 0
    assert mail.outbox == []


# ── Reminders ───────────────────────────────────────────────────────────────
def test_reminders_respect_the_configured_cadence(coop, plan, settings):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    mail.outbox.clear()

    # Same day: too soon, nothing sent.
    run_billing_cycle()
    assert mail.outbox == []

    # Past the interval: one reminder.
    invoice.last_reminder_at = timezone.now() - timedelta(
        days=settings.BILLING_REMINDER_INTERVAL_DAYS + 1)
    invoice.save(update_fields=["last_reminder_at"])
    run_billing_cycle()

    invoice.refresh_from_db()
    assert invoice.reminder_count == 1
    assert len(mail.outbox) == 1


def test_an_invoice_past_its_due_date_goes_overdue(coop, plan, settings):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    invoice.due_at = timezone.localdate() - timedelta(days=1)
    invoice.save(update_fields=["due_at"])

    run_billing_cycle()

    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.OVERDUE
    assert invoice.subscription.status == Subscription.Status.PAST_DUE


# ── Suspension ──────────────────────────────────────────────────────────────
def test_a_cooperative_is_not_suspended_while_merely_late(coop, plan, settings):
    """Late is not the same as abandoned — members keep their access."""
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    invoice.due_at = timezone.localdate() - timedelta(days=2)
    invoice.save(update_fields=["due_at"])

    run_billing_cycle()

    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.ACTIVE


def test_a_long_unpaid_invoice_suspends_the_cooperative(coop, plan, settings):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    invoice.due_at = timezone.localdate() - timedelta(
        days=settings.BILLING_SUSPEND_AFTER_DAYS + 1)
    invoice.status = Invoice.Status.OVERDUE
    invoice.save(update_fields=["due_at", "status"])

    report = run_billing_cycle()

    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.SUSPENDED
    assert report["suspended"] == [coop.name]


def test_suspension_is_audited_as_a_system_action(coop, plan, settings):
    """An automatic suspension must be as traceable as an admin's click."""
    from audit.models import AuditLog

    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    invoice.due_at = timezone.localdate() - timedelta(
        days=settings.BILLING_SUSPEND_AFTER_DAYS + 1)
    invoice.status = Invoice.Status.OVERDUE
    invoice.save(update_fields=["due_at", "status"])

    run_billing_cycle()

    entry = AuditLog.all_objects.filter(
        action="cooperative.suspend_unpaid").first()
    assert entry is not None
    assert entry.actor_label == "System"


# ── Payment ─────────────────────────────────────────────────────────────────
def test_paying_activates_the_subscription_and_rolls_the_period(coop, plan):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    before_end = invoice.subscription.current_period_end

    settle_invoice(invoice)

    invoice.refresh_from_db()
    sub = invoice.subscription
    sub.refresh_from_db()
    assert invoice.status == Invoice.Status.PAID
    assert sub.status == Subscription.Status.ACTIVE
    assert sub.current_period_end > before_end


def test_paying_restores_a_suspended_cooperative(coop, plan, settings):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    invoice.due_at = timezone.localdate() - timedelta(
        days=settings.BILLING_SUSPEND_AFTER_DAYS + 1)
    invoice.status = Invoice.Status.OVERDUE
    invoice.save(update_fields=["due_at", "status"])
    run_billing_cycle()
    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.SUSPENDED

    settle_invoice(invoice)

    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.ACTIVE


def test_settling_twice_is_harmless(coop, plan):
    _expire_trial(ensure_subscription(coop))
    run_billing_cycle()
    invoice = Invoice.objects.get()
    settle_invoice(invoice)
    end_after_first = invoice.subscription.current_period_end

    settle_invoice(invoice)

    invoice.subscription.refresh_from_db()
    assert invoice.subscription.current_period_end == end_after_first, (
        "a repeated webhook must not roll the period forward twice")


def test_only_active_subscriptions_count_as_revenue(coop, plan):
    """Ties the cycle back to the MRR fix: a trial is not revenue."""
    from platform_admin.services import subscription_mrr

    ensure_subscription(coop)
    assert subscription_mrr() == Decimal("0")

    sub = _expire_trial(Subscription.objects.get())
    run_billing_cycle()
    settle_invoice(Invoice.objects.get())

    sub.refresh_from_db()
    assert sub.status == Subscription.Status.ACTIVE
    assert subscription_mrr() == Decimal("10000")
