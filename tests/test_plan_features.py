"""
Plan features: what each subscription plan includes, enforced.

Every plan keeps the full system of record. Plans differ by electronic payouts,
dividends, savings plans, branding and a custom domain. Pinned here: new use of
a feature the plan lacks is refused with a message that names the plan to move
to; history stays readable; a society's own money is never locked in; and the
30-day grace keeps existing societies whole while they decide.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from core.entitlements import (CATALOGUE, CUSTOM_BRANDING, DIVIDENDS,
                               ELECTRONIC_PAYOUTS, features_for)
from platform_admin.models import Plan, Subscription

pytestmark = pytest.mark.django_db

GROWTH = ["custom_branding", "dividends", "electronic_payouts",
          "savings_plans"]


@pytest.fixture
def plans():
    return {
        "Starter": Plan.objects.create(name="Starter", min_members=50,
                                       max_members=250, features=[]),
        "Growth": Plan.objects.create(name="Growth", min_members=251,
                                      max_members=1000, features=GROWTH),
    }


def _on(coop, plan, *, grace_until=None):
    return Subscription.objects.create(
        cooperative=coop, plan=plan, status=Subscription.Status.ACTIVE,
        features_grace_until=grace_until)


def _officer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@x.co", full_name="Sec",
                                        password="x")
        Membership.objects.create(
            user=user, member_no="SEC-1",
            role=Role.objects.filter(slug="secretary").first())
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}",
                       HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


# ── Who gets what ───────────────────────────────────────────────────────────
def test_an_unbilled_society_keeps_everything(coop):
    assert features_for(coop) == set(CATALOGUE)


def test_features_follow_the_plan(coop, plans):
    _on(coop, plans["Starter"])
    assert features_for(coop) == set()


def test_the_grace_period_keeps_everything_until_it_ends(coop, plans):
    sub = _on(coop, plans["Starter"],
              grace_until=date.today() + timedelta(days=1))
    assert features_for(coop) == set(CATALOGUE)

    sub.features_grace_until = date.today() - timedelta(days=1)
    sub.save()
    assert features_for(coop) == set()


# ── Refusals name the way forward ──────────────────────────────────────────
def test_declaring_a_dividend_needs_the_plan(coop, plans):
    _on(coop, plans["Starter"])

    resp = _officer(coop).post("/api/v1/dividends/",
                               {"total_amount": "1000",
                                "period_label": "FY2026"}, format="json")

    assert resp.status_code == 403
    assert "from the Growth plan" in resp.json()["detail"]


def test_past_dividends_stay_readable(coop, plans):
    _on(coop, plans["Starter"])
    assert _officer(coop).get("/api/v1/dividends/").status_code == 200


def test_a_member_cannot_start_a_savings_goal_without_the_plan(coop, member,
                                                               plans):
    _on(coop, plans["Starter"])
    token, _ = Token.objects.get_or_create(user=member.user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}",
                       HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert client.get("/api/v1/me/savings-goals/").status_code == 200
    resp = client.post("/api/v1/me/savings-goals/",
                       {"name": "School fees", "target_amount": "50000"},
                       format="json")
    assert resp.status_code == 403


def test_the_same_request_works_on_a_plan_that_includes_it(coop, plans):
    _on(coop, plans["Growth"])

    resp = _officer(coop).post("/api/v1/dividends/",
                               {"total_amount": "1000",
                                "period_label": "FY2026"}, format="json")

    assert resp.status_code != 403


# ── Money ───────────────────────────────────────────────────────────────────
def test_funding_the_wallet_needs_the_plan(coop, plans):
    from payments.services import WalletError, initiate_wallet_topup

    _on(coop, plans["Starter"])
    with pytest.raises(WalletError, match="Electronic loan payouts"):
        initiate_wallet_topup(coop, amount="5000", email="t@x.co")


def test_withdrawing_the_wallet_is_never_gated(coop, plans, monkeypatch):
    """The float is the society's own money; a plan cannot lock it in."""
    from payments import services

    _on(coop, plans["Starter"])
    called = {}
    monkeypatch.setattr(services, "send_payout",
                        lambda **kw: called.setdefault("payout", kw))
    coop.bank_code, coop.bank_account_no = "044", "0123456789"
    coop.save()

    services.withdraw_wallet(coop, amount="100")

    assert called["payout"]["amount"] == "100"


def test_approval_on_a_manual_plan_waits_for_cash_without_alarming_anyone(
        coop, member, plans):
    from communications.models import Notification
    from loans.models import Loan, LoanProduct
    from loans.services import approve_loan

    _on(coop, plans["Starter"])
    with use_tenant(coop):
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("5"),
            max_amount=Decimal("100000"), max_term_months=6)
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("1000"),
                                   interest_rate=Decimal("5"), term_months=1)
        alerts = Notification.all_objects.count()

        loan = approve_loan(loan, approve=True, auto_disburse=True)

    assert loan.status == Loan.Status.APPROVED
    assert loan.auto_disbursement is None
    assert "cash or by bank transfer" in loan.auto_disbursement_error
    # Only the member's own "approved" notice; no officer alert.
    assert Notification.all_objects.count() - alerts <= 1


# ── What the apps are told ──────────────────────────────────────────────────
def test_the_apps_learn_the_plan_and_lose_off_plan_branding(coop, member,
                                                            plans):
    coop.brand_color = "#123456"
    coop.save(update_fields=["brand_color"])
    _on(coop, plans["Starter"])
    token, _ = Token.objects.get_or_create(user=member.user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    membership = client.get("/api/v1/me/").json()["memberships"][0]

    assert membership["plan"]["name"] == "Starter"
    assert membership["plan"]["features"] == []
    assert membership["cooperative_brand_color"] == "#0b4f3a"


def test_an_officer_cannot_rebrand_off_plan(coop, plans):
    _on(coop, plans["Starter"])

    resp = _officer(coop).patch(f"/api/v1/cooperatives/{coop.id}/",
                                {"brand_color": "#123456"}, format="json")

    assert resp.status_code == 403
    assert CATALOGUE[CUSTOM_BRANDING][0] in resp.json()["detail"]


def test_the_price_card_reads_the_same_features(plans):
    rows = APIClient().get("/api/v1/public/plans/").json()
    growth = next(r for r in rows if r["name"] == "Growth")

    assert {f["key"] for f in growth["features"]} == set(GROWTH)
    catalogue = APIClient().get("/api/v1/public/plan-catalogue/").json()
    assert catalogue["core"] and len(catalogue["features"]) == len(CATALOGUE)
    assert DIVIDENDS in {f["key"] for f in catalogue["features"]}
    assert ELECTRONIC_PAYOUTS in {f["key"] for f in catalogue["features"]}
