"""
A lapsed subscription: the console locks down to renewing.

Officers keep reading their records but cannot act until they pay, and the
renewal itself always works — it is the way out of the lock.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from platform_admin.billing import ensure_subscription, is_lapsed
from platform_admin.models import Invoice, Plan, Subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def sub(coop):
    Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                        price_monthly=Decimal("10000"))
    coop.contact_email = "officers@imole.coop"
    coop.save(update_fields=["contact_email"])
    return ensure_subscription(coop)


@pytest.fixture
def chair(coop):
    with use_tenant(coop):
        user = User.objects.create_user(
            email="chair@imole.test", full_name="Chair", password="x")
        return Membership.objects.create(
            user=user, member_no="CH-001",
            role=Role.objects.get(slug="chairperson"))


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


def _lapse(sub, status=Subscription.Status.PAST_DUE):
    sub.status = status
    sub.save(update_fields=["status", "updated_at"])


def test_what_counts_as_lapsed(sub):
    today = timezone.localdate()
    assert not is_lapsed(sub), "a running trial"

    sub.current_period_end = today
    assert is_lapsed(sub), "a trial whose end has come"

    sub.status = Subscription.Status.ACTIVE
    assert not is_lapsed(sub), "an active society on its renewal day"

    for status in (Subscription.Status.PAST_DUE,
                   Subscription.Status.CANCELED):
        sub.status = status
        assert is_lapsed(sub)
    assert not is_lapsed(None)


def test_officers_cannot_act_but_can_still_read(coop, sub, chair):
    _lapse(sub)
    client = _client(chair.user, coop)

    refused = client.post("/api/v1/verification/submit/")
    assert refused.status_code == 403
    assert "subscription has ended" in refused.json()["detail"]
    assert client.get("/api/v1/verification/").status_code == 200


def test_the_membership_payload_says_so(coop, sub, chair):
    _lapse(sub)
    client = _client(chair.user, coop)
    plan = client.get("/api/v1/me/").json()
    plan = next(m for m in plan["memberships"]
                if m["cooperative_id"] == coop.id)["plan"]
    assert plan["lapsed"] is True


def test_renewing_raises_one_invoice_and_returns_its_pay_link(coop, sub, chair):
    _lapse(sub)
    client = _client(chair.user, coop)

    first = client.post("/api/v1/subscription/renew/")
    assert first.status_code == 200, first.json()
    second = client.post("/api/v1/subscription/renew/")

    assert first.json()["pay_token"] == second.json()["pay_token"]
    assert Invoice.objects.filter(cooperative=coop).count() == 1
    assert first.json()["amount"] == "10000.00"


def test_a_cancelled_society_restarts_from_today(coop, sub, chair):
    sub.current_period_end = timezone.localdate() - timedelta(days=90)
    sub.save(update_fields=["current_period_end", "updated_at"])
    _lapse(sub, Subscription.Status.CANCELED)

    _client(chair.user, coop).post("/api/v1/subscription/renew/")
    sub.refresh_from_db()
    assert sub.current_period_end == timezone.localdate()


def test_an_ordinary_member_cannot_renew(coop, sub, member):
    _lapse(sub)
    response = _client(member.user, coop).post("/api/v1/subscription/renew/")
    assert response.status_code == 403
