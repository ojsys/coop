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
    return _add_months(d, 1)


def _add_months(d: date, n: int) -> date:
    """``n`` months on, clamped to the end of a short month.

    31 Jan + 1 month is 28 Feb, not 3 March: a billing period that silently
    slides forward a few days each month drifts off its own anniversary.

    Computed in one step, not month by month: stepping clamps at every short
    month on the way, so 29 Oct + 12 months came out as 28 Oct (via 28 Feb)
    and an annual period started on the 29th–31st ended days early.
    """
    total = d.month - 1 + n
    year, month = d.year + total // 12, total % 12 + 1
    return d.replace(year=year, month=month,
                     day=min(d.day, calendar.monthrange(year, month)[1]))


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
    from django.db import transaction

    with transaction.atomic():
        # Locked, because the browser return and the webhook arrive within
        # moments of each other: settling twice would roll the subscription
        # on two periods for one payment.
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        _settle_locked(locked, paid_on or timezone.localdate())
    # The caller's instance, brought up to date, so it reads as paid.
    invoice.refresh_from_db()
    return invoice


def _settle_locked(invoice, paid_on):
    if invoice.status == Invoice.Status.PAID:
        return

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

    # The cooperative's referrer, if any, earns their share. Never raises.
    from referrals.services import credit_for_invoice
    credit_for_invoice(invoice)


# ── The console's view of a renewal ─────────────────────────────────────────
LAPSED_STATUSES = (Subscription.Status.PAST_DUE, Subscription.Status.CANCELED)


def is_lapsed(subscription, *, today: date | None = None) -> bool:
    """Whether the society has stopped paying for the console.

    Past due or cancelled, or a free trial whose end has come without a paid
    invoice. An active subscription at the start of a new period is *not*
    lapsed: its invoice has days to run before it is due, and a society that
    pays every month should not be locked out on every renewal day.
    """
    if subscription is None:
        return False
    if subscription.status in LAPSED_STATUSES:
        return True
    today = today or timezone.localdate()
    return (subscription.status == Subscription.Status.TRIAL
            and subscription.current_period_end is not None
            and subscription.current_period_end <= today)


def outstanding_invoice(cooperative) -> Invoice | None:
    return _outstanding(cooperative).order_by("issued_at", "id").first()


def renewal_invoice(cooperative, *, today: date | None = None) -> Invoice:
    """The invoice an officer pays to renew, raising one if none is open.

    The daily cycle normally raises it. This covers the gap before the cycle
    has run, and a cancelled subscription, which the cycle never invoices.
    A cancelled one restarts from today rather than billing the months it
    was switched off.
    """
    from django.db import transaction

    today = today or timezone.localdate()
    with transaction.atomic():
        sub = (Subscription.objects.select_for_update()
               .select_related("plan", "cooperative")
               .filter(cooperative=cooperative).first())
        if sub is None:
            raise UnpricedSubscription(
                f"{cooperative.name} is not on a plan yet.")
        invoice = outstanding_invoice(cooperative)
        if invoice is not None:
            return invoice
        if sub.status == Subscription.Status.CANCELED:
            sub.current_period_end = today
            sub.save(update_fields=["current_period_end", "updated_at"])
        return open_invoice(sub, today=today)


def recheck_invoice(invoice) -> bool:
    """Ask Paystack whether an unsettled invoice was in fact paid.

    For a payment whose webhook never arrived and whose payer closed the
    browser before returning. Only an invoice that reached checkout has a
    reference to ask about. Returns whether it is now paid; never raises, so
    a Paystack outage cannot break the page or the cycle that asked.
    """
    from payments.providers import PaymentInitError
    from payments.services import verify_invoice_payment

    if invoice.is_settled or not invoice.psp_reference:
        return invoice.status == Invoice.Status.PAID
    # The dev placeholder key reports every charge as a success, which would
    # settle each invoice that merely reached checkout. Only a real key is
    # worth asking.
    secret = settings.PAYSTACK_SECRET_KEY
    if not secret or secret.startswith("sk_test_dev"):
        return False
    try:
        verify_invoice_payment(invoice.psp_reference)
    except PaymentInitError:
        return False
    invoice.refresh_from_db(fields=["status"])
    return invoice.status == Invoice.Status.PAID


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


TRIAL_REMINDER_DAYS = (7, 3, 1)


def _trial_mark(days_left: int) -> int | None:
    """The reminder a trial with ``days_left`` is due: the nearest mark at or
    above it, so a day the cycle did not run is caught up the next day."""
    marks = [m for m in TRIAL_REMINDER_DAYS if m >= days_left]
    return min(marks) if marks else None


def _trials_due_a_reminder(today: date, cooperative=None):
    subs = Subscription.objects.filter(
        status=Subscription.Status.TRIAL, current_period_end__gt=today,
        current_period_end__lte=today + timedelta(
            days=max(TRIAL_REMINDER_DAYS)),
    ).select_related("cooperative", "plan")
    if cooperative is not None:
        subs = subs.filter(cooperative=cooperative)
    for sub in subs:
        days_left = (sub.current_period_end - today).days
        mark = _trial_mark(days_left)
        if mark is None:
            continue
        if sub.trial_reminded_days is not None \
                and sub.trial_reminded_days <= mark:
            continue
        yield sub, days_left


def run_billing_cycle(*, dry_run: bool = False, cooperative=None,
                      today: date | None = None) -> dict:
    """One pass of the whole cycle. Safe to run daily; safe to run twice.

    Ordering matters: invoices are raised, then marked overdue, then reminded
    about, then acted on. Reminding before marking overdue would send the
    gentler wording on the day an account actually lapsed.
    """
    from communications.email import (
        send_subscription_invoice_email, send_trial_ending_email,
    )
    from tenants.services import set_cooperative_status

    today = today or timezone.localdate()
    report = {"rechecked": [], "trial_reminded": [], "issued": [],
              "reminded": [],
              "overdue": [], "suspended": [], "unpriced": [],
              "dry_run": dry_run}

    # Before anything else: invoices someone paid whose confirmation went
    #    astray. Settling them first stops the steps below reminding, marking
    #    overdue or suspending a society that has paid.
    for invoice in _outstanding(cooperative).exclude(psp_reference=""):
        if dry_run:
            continue
        if recheck_invoice(invoice):
            report["rechecked"].append(invoice.number)

    # 0. Trials about to end are told so, at a week, three days and a day out.
    for sub, days_left in _trials_due_a_reminder(today, cooperative):
        report["trial_reminded"].append(
            f"{sub.cooperative.name} · {days_left}d")
        if dry_run:
            continue
        if send_trial_ending_email(sub, days_left=days_left):
            sub.trial_reminded_days = _trial_mark(days_left)
            sub.save(update_fields=["trial_reminded_days", "updated_at"])

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
