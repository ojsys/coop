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
    # MRR is derived from each cooperative's real tier (both default Small).
    assert data["mrr"] == Decimal("20000")
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


def test_mrr_reflects_tier_mix(coop):
    """MRR moves with the real tier of each cooperative."""
    # Bump the seeded coop to Medium and add a Large tenant.
    Cooperative.objects.filter(pk=coop.pk).update(tier=Cooperative.Tier.MEDIUM)
    provisioned = Cooperative.objects.create(
        name="Institution FedCoop", slug="fed", tier=Cooperative.Tier.LARGE,
        status=Cooperative.Status.ACTIVE,
    )
    provisioned.seed_chart_of_accounts()

    data = platform_overview()
    # Medium (35,000) + Large (150,000) = 185,000
    assert data["mrr"] == Decimal("185000")
    assert data["by_tier"] == {"small": 0, "medium": 1, "large": 1}
