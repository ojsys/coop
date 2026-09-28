"""Cross-tenant platform analytics (Startup Ripple command center)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.models import Membership, User
from contributions.models import ContributionType
from contributions.services import record_contribution
from core.context import use_tenant
from tenants.models import Cooperative
from tenants.platform_service import platform_overview

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_coops(coop, other_coop, member, dues_type):
    """`coop` has a member who pays ₦5,000 dues; `other_coop` is a second tenant."""
    with use_tenant(coop):
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="5000",
                            channel="cash")
    # Give the second cooperative its own member so counts span tenants.
    with use_tenant(other_coop):
        u = User.objects.create_user(email="aba1@example.com", full_name="Aba One")
        Membership.objects.create(user=u, member_no="ABA-0001")
    return coop, other_coop


def test_overview_aggregates_across_tenants(two_coops):
    coop, other = two_coops
    data = platform_overview()

    # Both tenants are counted — this is deliberately cross-cooperative.
    assert data["cooperatives_live"] == 2
    assert data["members_total"] == 2
    # Gross contributions sum confirmed postings across all tenants.
    assert data["gross_contributions"] == Decimal("5000.00")
    # Nobody has been billed, so there is no revenue. This previously asserted
    # ₦20,000 — a per-tier estimate the old fallback invented — which is exactly
    # the figure that appeared on the live dashboard as "Platform MRR" before a
    # single subscription existed.
    assert data["mrr"] == Decimal("0")
    # By-state groups the real tenant book.
    assert {r["state"]: r["count"] for r in data["by_state"]}["Lagos"] >= 1
    assert data["by_tier"]["small"] == 2
    assert len(data["contributions_trend"]) == 6
    assert data["reconciliation_accuracy"] == 100.0


def test_overview_requires_platform_admin(two_coops):
    coop, _ = two_coops
    client = APIClient()

    # A cooperative official is NOT a platform admin → forbidden.
    with use_tenant(coop):
        secretary = User.objects.create_user(
            email="sec@imole.coop", full_name="Sec", password="x",
        )
        Membership.objects.create(user=secretary, member_no="IMC-0002")
    client.force_authenticate(secretary)
    assert client.get("/api/v1/platform/overview/").status_code == 403

    # A platform admin may read it.
    admin = User.objects.create_superuser(
        email="admin@startupripple.co", full_name="Admin", password="x",
    )
    client.force_authenticate(admin)
    resp = client.get("/api/v1/platform/overview/")
    assert resp.status_code == 200
    assert resp.data["cooperatives_live"] == 2


def test_mrr_reflects_subscriptions_not_tiers(coop):
    """Revenue comes from what cooperatives are billed, not what size they are.

    The replaced version asserted MRR moved with each cooperative's *tier*,
    which is the defect: a tier is a size band, not a payment.
    """
    from platform_admin.models import Plan, Subscription

    growth = Plan.objects.create(
        name="Growth", tier=Plan.Tier.MEDIUM, price_monthly=Decimal("35000"))
    big = Cooperative.objects.create(
        name="Institution FedCoop", slug="fed", tier=Cooperative.Tier.LARGE,
        status=Cooperative.Status.ACTIVE,
    )
    big.seed_chart_of_accounts()

    # A large tenant with no subscription contributes nothing.
    assert platform_overview()["mrr"] == Decimal("0")

    Subscription.objects.create(cooperative=coop, plan=growth,
                                status=Subscription.Status.ACTIVE)
    assert platform_overview()["mrr"] == Decimal("35000")


def test_trials_and_past_due_are_not_counted_as_revenue(coop, other_coop):
    """A trial pays nothing and a past-due account has not paid.

    Counting either overstates MRR exactly when it is most misleading — at the
    start, when nearly every subscription is a trial.
    """
    from platform_admin.models import Plan, Subscription
    from platform_admin.services import subscription_breakdown

    starter = Plan.objects.create(
        name="Starter", tier=Plan.Tier.SMALL, price_monthly=Decimal("10000"))
    Subscription.objects.create(cooperative=coop, plan=starter,
                                status=Subscription.Status.TRIAL)
    Subscription.objects.create(cooperative=other_coop, plan=starter,
                                status=Subscription.Status.PAST_DUE)

    assert platform_overview()["mrr"] == Decimal("0")

    # Not hidden, just not counted as revenue.
    breakdown = subscription_breakdown()
    assert breakdown["trial"]["count"] == 1
    assert breakdown["past_due"]["count"] == 1
    assert breakdown["past_due"]["value"] == Decimal("10000")
