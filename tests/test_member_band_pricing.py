"""
The member-band price list and annual billing (October 2026 pricing audit).

The audit's findings, pinned: a society is placed by its **active member
count**, never by a size label that disagreed with the bands; annual is a real
billing cycle — one invoice for twelve months at the annual price — and revenue
figures count an annual subscription at a twelfth of what it pays.
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

import pytest
from django.apps import apps as django_apps

from accounts.models import Membership, User
from platform_admin.billing import (UnpricedSubscription, open_invoice,
                                    plan_for, run_billing_cycle,
                                    settle_invoice)
from platform_admin.models import Plan, Subscription
from platform_admin.services import subscription_mrr

pytestmark = pytest.mark.django_db

BANDS = [
    ("Starter", "9000", "90000", 50, 250, False),
    ("Growth", "15000", "150000", 251, 1000, False),
    ("Professional", "29000", "290000", 1001, 5000, False),
    ("Institutional", "49000", None, 5001, 0, True),
]


@pytest.fixture
def price_list():
    return {
        name: Plan.objects.create(
            name=name, price_monthly=Decimal(m),
            price_annual=Decimal(a) if a else None, price_is_from=is_from,
            min_members=lo, max_members=hi)
        for name, m, a, lo, hi, is_from in BANDS
    }


def _members(coop, n):
    for i in range(n):
        user = User.objects.create_user(email=f"band{i}@x.co",
                                        full_name=f"B {i}", password="x")
        Membership.all_objects.create(cooperative=coop, user=user,
                                      member_no=f"B-{i}",
                                      status=Membership.Status.ACTIVE)


# ── Placement by band ──────────────────────────────────────────────────────
def test_a_new_society_below_every_band_starts_on_starter(coop, price_list):
    assert plan_for(coop) == price_list["Starter"]


def test_a_society_is_placed_by_its_active_member_count(coop, price_list):
    for plan in price_list.values():   # shrink bands so the test stays small
        plan.min_members //= 50
        plan.max_members //= 50
        plan.save()
    _members(coop, 7)                  # Growth is now 5–20

    assert plan_for(coop) == price_list["Growth"]


def test_the_band_label_is_written_one_way(price_list):
    assert price_list["Starter"].band_label == "50–250 members"
    assert price_list["Institutional"].band_label == "5,001+ members"


# ── Annual billing ─────────────────────────────────────────────────────────
def _subscription(coop, plan, cycle, **kw):
    return Subscription.objects.create(
        cooperative=coop, plan=plan, billing_cycle=cycle,
        status=Subscription.Status.ACTIVE,
        current_period_start=date(2026, 9, 10),
        current_period_end=date(2026, 10, 10), **kw)


def test_an_annual_invoice_covers_twelve_months_at_the_annual_price(
        coop, price_list):
    sub = _subscription(coop, price_list["Growth"],
                        Subscription.Cycle.ANNUAL)

    invoice = open_invoice(sub, today=date(2026, 10, 10))

    assert invoice.amount == Decimal("150000.00")
    assert invoice.period_label == "Oct 2026 – Sep 2027"


def test_paying_an_annual_invoice_renews_for_a_year(coop, price_list):
    sub = _subscription(coop, price_list["Growth"],
                        Subscription.Cycle.ANNUAL)
    invoice = open_invoice(sub, today=date(2026, 10, 10))

    settle_invoice(invoice, paid_on=date(2026, 10, 12))

    sub.refresh_from_db()
    assert sub.current_period_start == date(2026, 10, 10)
    assert sub.current_period_end == date(2027, 10, 10)


def test_monthly_still_bills_the_monthly_price(coop, price_list):
    sub = _subscription(coop, price_list["Starter"],
                        Subscription.Cycle.MONTHLY)

    invoice = open_invoice(sub, today=date(2026, 10, 10))

    assert invoice.amount == Decimal("9000.00")
    assert invoice.period_label == "Oct 2026"


def test_a_quoted_annual_price_is_never_guessed(coop, price_list):
    sub = _subscription(coop, price_list["Institutional"],
                        Subscription.Cycle.ANNUAL)

    with pytest.raises(UnpricedSubscription):
        open_invoice(sub)
    report = run_billing_cycle(today=date(2026, 10, 10))
    assert report["unpriced"] and not report["issued"]


def test_a_negotiated_price_is_what_gets_invoiced(coop, price_list):
    sub = _subscription(coop, price_list["Institutional"],
                        Subscription.Cycle.ANNUAL,
                        price_override=Decimal("540000"))

    assert open_invoice(sub).amount == Decimal("540000.00")


# ── Revenue figures ─────────────────────────────────────────────────────────
def test_an_annual_subscription_counts_a_twelfth_towards_mrr(coop,
                                                            price_list):
    _subscription(coop, price_list["Growth"], Subscription.Cycle.ANNUAL)

    assert subscription_mrr() == Decimal("12500.00")


def test_a_price_change_reaches_mrr(coop, price_list):
    _subscription(coop, price_list["Starter"], Subscription.Cycle.MONTHLY)
    plan = price_list["Starter"]
    plan.price_monthly = Decimal("9500")
    plan.save()

    assert subscription_mrr() == Decimal("9500.00")


# ── The platform API ────────────────────────────────────────────────────────
def test_annual_on_a_quoted_plan_needs_the_quote(coop, price_list):
    from platform_admin.serializers import SubscriptionSerializer

    data = {"cooperative": coop.id, "plan": price_list["Institutional"].id,
            "status": "active", "billing_cycle": "annual"}
    assert not SubscriptionSerializer(data=data).is_valid()
    assert SubscriptionSerializer(
        data={**data, "price_override": "540000"}).is_valid()


def test_the_public_feed_carries_annual_prices_and_bands(price_list):
    from rest_framework.test import APIClient

    rows = APIClient().get("/api/v1/public/plans/").data

    assert [r["name"] for r in rows] == [
        "Starter", "Growth", "Professional", "Institutional"]
    assert rows[0]["price_annual"] == "90000.00"
    assert rows[0]["band_label"] == "50–250 members"
    assert rows[3]["price_annual"] is None
    assert rows[3]["price_is_from"] is True


# ── The migration, on a database that already has a price list ─────────────
def test_existing_subscribers_move_to_their_band_at_the_new_price(coop):
    old = Plan.objects.create(name="Pro", price_monthly=Decimal("12000"),
                              min_members=5000, max_members=10000)
    sub = Subscription.objects.create(cooperative=coop, plan=old,
                                      status=Subscription.Status.ACTIVE)
    migration = importlib.import_module(
        "platform_admin.migrations.0025_member_band_pricing")

    migration.forwards(django_apps, None)

    sub.refresh_from_db()
    old.refresh_from_db()
    assert sub.plan.name == "Starter", "a society with no members yet"
    assert sub.monthly_value == Decimal("9000.00")
    assert old.active is False, "retired, not deleted — invoices point at it"
    assert set(Plan.objects.filter(active=True).values_list(
        "name", flat=True)) == {"Starter", "Growth", "Professional",
                                "Institutional"}
