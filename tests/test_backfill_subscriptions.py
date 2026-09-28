"""
Backfilling subscriptions for cooperatives that went live before billing did.

The property that matters most is that the free month starts *now*. Backdating
to the original go-live date would make a long-standing tenant instantly due
and, past the suspension threshold, instantly suspendable — for a subscription
nobody had ever mentioned to them.

Assertions are on database state rather than printed output, so the command's
wording can change without these needing to.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from platform_admin.models import Plan, Subscription
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.fixture
def starter():
    return Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                               price_monthly=Decimal("10000"))


def _live(slug, tier=Cooperative.Tier.SMALL):
    coop = Cooperative.objects.create(
        name=slug.title(), slug=slug, tier=tier,
        status=Cooperative.Status.ACTIVE,
    )
    coop.seed_chart_of_accounts()
    return coop


def test_a_live_cooperative_gets_a_trial(starter):
    coop = _live("unity-farmers")

    call_command("backfill_subscriptions")

    sub = Subscription.objects.get(cooperative=coop)
    assert sub.status == Subscription.Status.TRIAL
    assert sub.plan == starter


def test_the_free_month_starts_today_not_at_go_live(starter):
    """The whole point: a tenant live for a year must not be instantly due."""
    coop = _live("aba-traders")

    call_command("backfill_subscriptions")

    sub = Subscription.objects.get(cooperative=coop)
    today = timezone.localdate()
    assert sub.current_period_start == today
    assert sub.current_period_end > today, (
        "backdating would make them due — and possibly suspendable — on day one"
    )


def test_it_is_safe_to_run_twice(starter):
    coop = _live("kano-millers")
    call_command("backfill_subscriptions")
    first = Subscription.objects.get(cooperative=coop)

    call_command("backfill_subscriptions")

    assert Subscription.objects.filter(cooperative=coop).count() == 1
    assert Subscription.objects.get(cooperative=coop).pk == first.pk


def test_dry_run_writes_nothing(starter):
    _live("coastal-fishers")

    call_command("backfill_subscriptions", "--dry-run")

    assert Subscription.objects.count() == 0


def test_cooperatives_that_are_not_live_are_left_alone(starter):
    Cooperative.objects.create(name="Prospect", slug="prospect",
                               status=Cooperative.Status.PROSPECTIVE)
    Cooperative.objects.create(name="Gone", slug="gone",
                               status=Cooperative.Status.CLOSED)

    call_command("backfill_subscriptions")

    assert Subscription.objects.count() == 0


def test_a_tier_with_no_plan_is_skipped_not_guessed(starter):
    """A Large cooperative with only a Small plan configured must not be put
    on the wrong price."""
    big = _live("fed-institution", tier=Cooperative.Tier.LARGE)

    call_command("backfill_subscriptions")

    assert not Subscription.objects.filter(cooperative=big).exists()


def test_one_cooperative_can_be_targeted(starter):
    a = _live("first-coop")
    b = _live("second-coop")

    call_command("backfill_subscriptions", "--cooperative", "first-coop")

    assert Subscription.objects.filter(cooperative=a).exists()
    assert not Subscription.objects.filter(cooperative=b).exists()


def test_a_backfilled_cooperative_is_not_revenue_until_it_pays(starter):
    """It joins the cycle as a trial, so MRR must not jump on backfill."""
    from platform_admin.services import subscription_mrr

    _live("unity-farmers")

    call_command("backfill_subscriptions")

    assert subscription_mrr() == Decimal("0")
