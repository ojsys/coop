"""
The subscription billing cycle.

A cooperative goes live, operates free for ``BILLING_TRIAL_DAYS``, and is then
invoiced monthly or annually, per its subscription's billing cycle. While an invoice is outstanding it is reminded every
``BILLING_REMINDER_INTERVAL_DAYS``; if it is still unpaid
``BILLING_SUSPEND_AFTER_DAYS`` past the due date, the cooperative is suspended.

Kept apart from ``services.py`` because that module reports on the business and
this one *changes* it — it raises invoices, sends mail and suspends tenants.
Every entry point takes ``dry_run`` so the whole cycle can be inspected before
it is trusted.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from platform_admin.models import Invoice, Plan, Subscription, make_pay_token

ZERO = Decimal("0.00")


def _add_month(d: date) -> date:
    """One month on, clamped to the end of a short month.

    31 Jan + 1 month is 28 Feb, not 3 March: a billing period that silently
    slides forward a few days each month drifts off its own anniversary.
    """
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return d.replace(year=year, month=month,
                     day=min(d.day, calendar.monthrange(year, month)[1]))


def _add_months(d: date, n: int) -> date:
    for _ in range(n):
        d = _add_month(d)
    return d


def active_members(cooperative) -> int:
    from accounts.models import Membership

    return Membership.all_objects.filter(
        cooperative=cooperative, status=Membership.Status.ACTIVE).count()


def plan_for(cooperative) -> Plan | None:
    """The active plan whose member band holds this cooperative.

    Chosen by active member count, the thing a buyer can verify, rather than
    by the cooperative's size label — the labels and bands used to disagree.
    A society below the smallest band — a new one still adding its members —
    starts on the entry plan. One above every configured band gets ``None``:
    putting it on a cheaper plan would be guessing at its price.
    """
    plans = list(Plan.objects.filter(active=True)
                 .order_by("min_members", "price_monthly"))
    if not plans:
        return None
    members = active_members(cooperative)
    if members < plans[0].min_members:
        return plans[0]
    return next((p for p in plans if p.holds(members)), None)


def ensure_subscription(cooperative, *, today: date | None = None):
    """Give a cooperative its free trial subscription. Idempotent.

    Returns the Subscription, or ``None`` when no plan matches its tier — which
    must not be fatal. Going live is the cooperative's moment; refusing to
    activate it because the platform has not configured a price list would put
    our billing problem in front of their members.
    """
    existing = Subscription.objects.filter(cooperative=cooperative).first()
    if existing is not None:
        return existing

    plan = plan_for(cooperative)
    if plan is None:
        return None

    today = today or timezone.localdate()
    return Subscription.objects.create(
        cooperative=cooperative,
        plan=plan,
        status=Subscription.Status.TRIAL,
        started_at=today,
        current_period_start=today,
        current_period_end=today + timedelta(days=settings.BILLING_TRIAL_DAYS),
    )


def _period_months(subscription) -> int:
    return 12 if subscription.is_annual else 1


def _period_label(start: date, months: int = 1) -> str:
    if months == 1:
        return start.strftime("%b %Y")
    last = _add_months(start, months - 1)
    return f"{start.strftime('%b %Y')} – {last.strftime('%b %Y')}"


class UnpricedSubscription(Exception):
    """An annual subscription whose price has to be quoted and has not been."""


def open_invoice(subscription, *, today: date | None = None) -> Invoice:
    """Raise the invoice for the period that has just begun.

    One month or twelve, per the subscription's billing cycle. Raises
    :class:`UnpricedSubscription` rather than inventing a figure when an
    annual price is quoted per society and none has been recorded.
    """
    from platform_admin.services import next_invoice_number

    amount = subscription.cycle_amount
    if amount is None:
        raise UnpricedSubscription(
            f"{subscription.cooperative.name} is on annual billing for "
            f"{subscription.plan.name}, which is quoted per society, and no "
            f"negotiated price is recorded.")

    today = today or timezone.localdate()
    start = subscription.current_period_end or today
    return Invoice.objects.create(
        cooperative=subscription.cooperative,
        subscription=subscription,
        number=next_invoice_number(),
        period_label=_period_label(start, _period_months(subscription)),
        amount=amount,
        currency=subscription.plan.currency,
        status=Invoice.Status.SENT,
        issued_at=today,
        due_at=today + timedelta(days=settings.BILLING_DUE_DAYS),
        pay_token=make_pay_token(),
    )


def settle_invoice(invoice, *, paid_on: date | None = None) -> Invoice:
    """Mark an invoice paid and roll its subscription on one billing period.

    The single path used by the platform admin's "mark paid" action and by the
    Paystack verification, so a bank transfer and a card payment leave the
    account in exactly the same state.
    """
    paid_on = paid_on or timezone.localdate()
    if invoice.status == Invoice.Status.PAID:
        return invoice

    invoice.status = Invoice.Status.PAID
    invoice.paid_at = paid_on
    invoice.save(update_fields=["status", "paid_at", "updated_at"])

    sub = invoice.subscription
    if sub is not None:
        sub.status = Subscription.Status.ACTIVE
        start = sub.current_period_end or paid_on
        sub.current_period_start = start
        sub.current_period_end = _add_months(start, _period_months(sub))
        sub.save(update_fields=["status", "current_period_start",
                                "current_period_end", "updated_at"])

        # A suspended cooperative that has now paid gets its access back.
        coop = sub.cooperative
        if coop.status == coop.Status.SUSPENDED:
            from tenants.services import set_cooperative_status

            set_cooperative_status(coop, coop.Status.ACTIVE,
                                   verb="reactivate", actor=None)
    return invoice


def _needs_reminder(cutoff):
    """Invoices never reminded about, or not reminded since ``cutoff``."""
    from django.db.models import Q

    return Q(last_reminder_at__isnull=True) | Q(last_reminder_at__lt=cutoff)


def _outstanding(cooperative=None):
    qs = Invoice.objects.filter(
        status__in=[Invoice.Status.SENT, Invoice.Status.OVERDUE],
    ).select_related("cooperative", "subscription", "subscription__plan")
    if cooperative is not None:
        qs = qs.filter(cooperative=cooperative)
    return qs


def run_billing_cycle(*, dry_run: bool = False, cooperative=None,
                      today: date | None = None) -> dict:
    """One pass of the whole cycle. Safe to run daily; safe to run twice.

    Ordering matters: invoices are raised, then marked overdue, then reminded
    about, then acted on. Reminding before marking overdue would send the
    gentler wording on the day an account actually lapsed.
    """
    from communications.email import send_subscription_invoice_email
    from tenants.services import set_cooperative_status

    today = today or timezone.localdate()
    report = {"issued": [], "reminded": [], "overdue": [], "suspended": [],
              "unpriced": [], "dry_run": dry_run}

    # 1. Periods that have ended get an invoice — unless one is already open,
    #    which is what makes a second run in the same day harmless.
    subs = Subscription.objects.filter(
        status__in=[Subscription.Status.TRIAL, Subscription.Status.ACTIVE,
                    Subscription.Status.PAST_DUE],
        current_period_end__lte=today,
    ).select_related("cooperative", "plan")
    if cooperative is not None:
        subs = subs.filter(cooperative=cooperative)

    for sub in subs:
        if _outstanding(sub.cooperative).exists():
            continue
        if sub.cycle_amount is None:
            # Never invoice a guessed figure; surface it for a quote instead.
            report["unpriced"].append(
                f"{sub.cooperative.name} · {sub.plan.name}")
            continue
        report["issued"].append(f"{sub.cooperative.name} · {sub.plan.name}")
        if dry_run:
            continue
        invoice = open_invoice(sub, today=today)
        sent = send_subscription_invoice_email(invoice)
        if sent:
            invoice.last_reminder_at = timezone.now()
            invoice.save(update_fields=["last_reminder_at", "updated_at"])

    # 2. Anything past its due date is overdue, and its subscription past due.
    for invoice in _outstanding(cooperative).filter(
            status=Invoice.Status.SENT, due_at__lt=today):
        report["overdue"].append(invoice.number)
        if dry_run:
            continue
        invoice.status = Invoice.Status.OVERDUE
        invoice.save(update_fields=["status", "updated_at"])
        sub = invoice.subscription
        if sub is not None and sub.status != Subscription.Status.PAST_DUE:
            sub.status = Subscription.Status.PAST_DUE
            sub.save(update_fields=["status", "updated_at"])

    # 3. Remind, but only on the configured cadence.
    cutoff = timezone.now() - timedelta(
        days=settings.BILLING_REMINDER_INTERVAL_DAYS)
    due_a_reminder = _outstanding(cooperative).filter(_needs_reminder(cutoff))
    for invoice in due_a_reminder:
        suspend_on = (invoice.due_at
                      + timedelta(days=settings.BILLING_SUSPEND_AFTER_DAYS)
                      if invoice.due_at else None)
        report["reminded"].append(invoice.number)
        if dry_run:
            continue
        if send_subscription_invoice_email(invoice, is_reminder=True,
                                           suspend_on=suspend_on):
            invoice.last_reminder_at = timezone.now()
            invoice.reminder_count += 1
            invoice.save(update_fields=["last_reminder_at", "reminder_count",
                                        "updated_at"])

    # 4. Suspend only after the long grace period — this locks members out of
    #    their own records, so it is the last step and the slowest.
    limit = today - timedelta(days=settings.BILLING_SUSPEND_AFTER_DAYS)
    for invoice in _outstanding(cooperative).filter(
            status=Invoice.Status.OVERDUE, due_at__lt=limit):
        coop = invoice.cooperative
        if coop.status != coop.Status.ACTIVE:
            continue
        report["suspended"].append(coop.name)
        if dry_run:
            continue
        set_cooperative_status(coop, coop.Status.SUSPENDED,
                               verb="suspend_unpaid", actor=None)

    return report
