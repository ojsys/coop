"""
Non-members: people a society serves who have not joined it.

They borrow, save, use the app and receive announcements, and they count
towards the society's pricing band like anyone else it serves. They hold no
shares, so they cannot vote, hold office or receive dividends. Loan products
say whether they may apply and at what rate. An officer adds them and can
convert one to a member.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from governance.models import Resolution
from governance.services import GovernanceError, cast_vote, open_resolution
from loans.models import Loan, LoanProduct

pytestmark = pytest.mark.django_db


@pytest.fixture
def outsider(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="customer@x.co",
                                        full_name="Walk-in Customer",
                                        password="x")
        return Membership.objects.create(user=user, member_no="NM-1",
                                         kind=Membership.Kind.NON_MEMBER)


def _client(user, coop):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}",
                       HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


def _officer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@x.co", full_name="Sec",
                                        password="x")
        Membership.objects.create(
            user=user, member_no="SEC-1",
            role=Role.objects.filter(slug=Role.SECRETARY).first())
    return _client(user, coop)


@pytest.fixture
def products(coop):
    with use_tenant(coop):
        members_only = LoanProduct.objects.create(
            name="Members Loan", interest_rate=Decimal("5"),
            max_amount=Decimal("500000"), max_term_months=12)
        open_to_all = LoanProduct.objects.create(
            name="Trader Loan", interest_rate=Decimal("5"),
            max_amount=Decimal("500000"), max_term_months=12,
            open_to_non_members=True,
            non_member_interest_rate=Decimal("8"))
    return members_only, open_to_all


# ── What they cannot do ─────────────────────────────────────────────────────
def test_a_non_member_cannot_vote(coop, outsider):
    with use_tenant(coop):
        res = Resolution.objects.create(title="Raise dues")
        open_resolution(res)
        with pytest.raises(GovernanceError, match="Non-members"):
            cast_vote(res, outsider, "for")


def test_a_non_member_gets_no_dividend(coop, outsider):
    from dividends.services import _eligible_members

    Membership.all_objects.filter(pk=outsider.pk).update(
        share_capital=Decimal("50000"))      # even with a stray figure
    assert outsider.pk not in {m.pk for m in _eligible_members(coop)}


def test_a_non_member_cannot_hold_office_or_shares(coop, outsider):
    client = _officer(coop)
    officer_role = Role.all_objects.filter(cooperative=coop,
                                       slug=Role.TREASURER).first()

    resp = client.patch(f"/api/v1/members/{outsider.id}/",
                        {"role": officer_role.id}, format="json")
    assert resp.status_code == 400
    resp = client.patch(f"/api/v1/members/{outsider.id}/",
                        {"share_capital": "1000"}, format="json")
    assert resp.status_code == 400


def test_kind_is_not_flipped_by_a_field_edit(coop, outsider):
    resp = _officer(coop).patch(f"/api/v1/members/{outsider.id}/",
                                {"kind": "member"}, format="json")
    assert resp.status_code == 400


# ── Loans ───────────────────────────────────────────────────────────────────
def test_a_non_member_sees_and_applies_only_for_open_products(
        coop, outsider, products):
    members_only, open_to_all = products
    client = _client(outsider.user, coop)

    listed = client.get("/api/v1/me/loan-products/").json()
    listed = listed["results"] if isinstance(listed, dict) else listed
    assert [p["name"] for p in listed] == ["Trader Loan"]

    refused = client.post("/api/v1/me/loans/", {
        "product": members_only.id, "principal": "10000",
        "term_months": 3}, format="json")
    assert refused.status_code == 400
    assert "members only" in str(refused.json())

    ok = client.post("/api/v1/me/loans/", {
        "product": open_to_all.id, "principal": "10000",
        "term_months": 3}, format="json")
    assert ok.status_code == 201, ok.content
    assert ok.json()["interest_rate"] == "8.00", "the non-member rate"


def test_members_keep_the_member_rate(coop, member, products):
    _, open_to_all = products
    resp = _client(member.user, coop).post("/api/v1/me/loans/", {
        "product": open_to_all.id, "principal": "10000",
        "term_months": 3}, format="json")
    assert resp.json()["interest_rate"] == "5.00"


# ── Joining, converting, counting ──────────────────────────────────────────
def test_an_officer_adds_a_non_member_and_converts_them(coop):
    client = _officer(coop)
    created = client.post("/api/v1/members/", {
        "member_no": "NM-9", "full_name": "Trader", "email": "t@x.co",
        "kind": "non_member"}, format="json")
    assert created.status_code == 201, created.content
    assert created.json()["kind_display"] == "Non-member"

    converted = client.post(
        f"/api/v1/members/{created.json()['id']}/convert-to-member/")
    assert converted.status_code == 200
    assert converted.json()["kind"] == "member"


def test_non_members_count_towards_the_pricing_band(coop, outsider):
    from platform_admin.billing import active_members

    before = Membership.all_objects.filter(
        cooperative=coop, status=Membership.Status.ACTIVE).count()
    assert active_members(coop) == before


def test_the_app_knows_who_is_a_non_member(coop, outsider):
    me = _client(outsider.user, coop).get("/api/v1/me/").json()
    assert me["memberships"][0]["kind"] == "non_member"


# ── Part 1: the console's plan picture ─────────────────────────────────────
def test_the_console_sees_the_billing_plan_not_the_size_label(coop, member):
    from platform_admin.models import Plan, Subscription

    plan = Plan.objects.create(name="Starter", min_members=50,
                               max_members=250, price_monthly=Decimal("9000"),
                               price_annual=Decimal("90000"))
    Subscription.objects.create(cooperative=coop, plan=plan,
                                billing_cycle="annual",
                                status=Subscription.Status.ACTIVE)

    info = _client(member.user, coop).get("/api/v1/me/").json()
    info = info["memberships"][0]["plan"]
    assert info["name"] == "Starter"
    assert info["band_label"] == "50–250 members"
    assert info["max_members"] == 250
    assert info["cycle_amount"] == "90000.00"
    assert info["members_counted"] >= 1


def test_officers_cannot_set_their_own_size_or_cap(coop):
    client = _officer(coop)
    client.patch(f"/api/v1/cooperatives/{coop.id}/",
                 {"member_cap": 99999, "tier": "large"}, format="json")
    coop.refresh_from_db()
    assert coop.member_cap != 99999
    assert coop.tier != "large"
