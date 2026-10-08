"""
Dividends: declared from surplus, approved by a second officer, posted to
members' savings, and corrected by reversal.

    DRAFT ──approve──▶ POSTED ──reverse──▶ REVERSED

The society in these tests has earned ₦50,000 (income, no expenses), so that
is what may be distributed. Two shareholders hold ₦60,000 and ₦40,000.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from approvals.models import ApprovalRequest
from approvals.services import ApprovalError, decide_request
from communications.models import Notification
from core.context import use_tenant
from dividends.models import DividendAllocation, DividendDeclaration
from dividends.services import (DividendError, declare_dividend,
                                distributable_surplus, post_dividend,
                                reverse_dividend)
from ledger.models import Account
from ledger.services import Line, member_balance, post_journal
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _officer(coop, email, slug=Role.SECRETARY):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name=email.split("@")[0],
                                     password="x")
        Membership.objects.create(user=u, member_no=email[:6],
                                  share_capital="0",
                                  role=Role.objects.filter(slug=slug).first())
    return u


def _client(user, coop):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}",
                  HTTP_X_COOPERATIVE_ID=str(coop.id))
    return c


@pytest.fixture
def earned(coop):
    """₦50,000 of income — the surplus there is to distribute."""
    with use_tenant(coop):
        cash = Account.all_objects.get(cooperative=coop, code="1000")
        income, _ = Account.all_objects.get_or_create(
            cooperative=coop, code="4100",
            defaults={"name": "Loan Interest Income",
                      "kind": Account.Kind.INCOME, "system": True})
        post_journal(cooperative=coop, reference="EARN-1", memo="interest",
                     lines=[Line(account=cash, debit=Decimal("50000")),
                            Line(account=income, credit=Decimal("50000"))])
    return coop


@pytest.fixture
def two_shareholders(coop):
    with use_tenant(coop):
        u1 = User.objects.create_user(email="a@x.co", full_name="A")
        m1 = Membership.objects.create(user=u1, member_no="M-1",
                                       share_capital=Decimal("60000"))
        u2 = User.objects.create_user(email="b@x.co", full_name="B")
        m2 = Membership.objects.create(user=u2, member_no="M-2",
                                       share_capital=Decimal("40000"))
    return m1, m2


def _allocs(declaration):
    # Unscoped: the tenant-scoped reverse manager is empty outside a tenant.
    return DividendAllocation.all_objects.filter(declaration=declaration)


def _declare(coop, **kw):
    kw.setdefault("period_label", "FY2025")
    if "rate" not in kw:
        kw.setdefault("total_amount", "10000")
    with use_tenant(coop):
        return declare_dividend(cooperative=coop, **kw)


# ── Declaring ───────────────────────────────────────────────────────────────
def test_a_pool_is_shared_pro_rata_to_share_capital(earned, two_shareholders):
    m1, m2 = two_shareholders
    d = _declare(earned)

    allocs = {a.membership_id: a.amount for a in _allocs(d)}
    assert allocs[m1.id] == Decimal("6000.00")
    assert allocs[m2.id] == Decimal("4000.00")
    assert d.status == DividendDeclaration.Status.DRAFT


def test_a_rate_pays_that_share_of_each_members_capital(earned,
                                                        two_shareholders):
    m1, m2 = two_shareholders
    d = _declare(earned, rate="10")

    allocs = {a.membership_id: a.amount for a in _allocs(d)}
    assert allocs == {m1.id: Decimal("6000.00"), m2.id: Decimal("4000.00")}
    assert d.total_amount == Decimal("10000.00")
    assert d.method == DividendDeclaration.Method.RATE


def test_pool_rounding_lands_on_the_largest_allocation(earned, coop):
    with use_tenant(coop):
        for n in range(3):
            u = User.objects.create_user(email=f"r{n}@x.co", full_name="R")
            Membership.objects.create(user=u, member_no=f"R-{n}",
                                      share_capital=Decimal("100"))
    d = _declare(coop, total_amount="100")

    assert sum(a.amount for a in _allocs(d)) == Decimal("100.00")


def test_more_than_the_surplus_cannot_be_declared(earned, two_shareholders):
    """Paid from earnings — never from members' savings or share capital."""
    with pytest.raises(DividendError, match="surplus is available"):
        _declare(earned, total_amount="50000.01")


def test_with_no_surplus_nothing_can_be_declared(coop, two_shareholders):
    assert distributable_surplus(coop)["available"] == Decimal("0.00")
    with pytest.raises(DividendError, match="surplus"):
        _declare(coop)


def test_the_same_period_cannot_be_declared_twice(earned, two_shareholders):
    _declare(earned)
    with pytest.raises(DividendError, match="already exists"):
        _declare(earned, period_label="fy2025")


# ── Approval and posting ────────────────────────────────────────────────────
def test_posting_needs_a_second_officer(earned, two_shareholders):
    maker = _officer(earned, "sec@x.co")
    checker = _officer(earned, "tre@x.co", slug=Role.TREASURER)
    d = _declare(earned, created_by=maker)

    resp = _client(maker, earned).post(
        f"/api/v1/dividends/{d.id}/request-posting/")
    assert resp.status_code == 200, resp.content
    assert resp.json()["awaiting_approval"] is True
    req = ApprovalRequest.all_objects.get(
        action=ApprovalRequest.Action.DIVIDEND_POST, object_id=d.id)

    with pytest.raises(ApprovalError, match="different officer"):
        decide_request(req, actor=maker, approve=True)
    decide_request(req, actor=checker, approve=True)

    d.refresh_from_db()
    assert d.status == DividendDeclaration.Status.POSTED


def test_posting_credits_savings_and_balances_the_ledger(earned,
                                                         two_shareholders):
    m1, m2 = two_shareholders
    d = _declare(earned)
    post_dividend(d)

    assert member_balance(m1) == Decimal("6000.00")
    assert member_balance(m2) == Decimal("4000.00")
    assert trial_balance(earned)["balanced"] is True
    assert distributable_surplus(earned)["available"] == Decimal("40000.00")


def test_members_are_told_what_reached_their_savings(earned,
                                                     two_shareholders):
    m1, _ = two_shareholders
    post_dividend(_declare(earned))

    note = Notification.all_objects.get(membership=m1,
                                        kind=Notification.Kind.DIVIDEND)
    assert "₦6,000.00" in note.body


def test_two_drafts_cannot_together_exceed_the_surplus(earned,
                                                       two_shareholders):
    first = _declare(earned, total_amount="30000")
    second = _declare(earned, period_label="FY2025-B", total_amount="30000")
    post_dividend(first)

    with pytest.raises(DividendError, match="surplus"):
        post_dividend(second)


# ── Reversal ────────────────────────────────────────────────────────────────
def test_a_posted_dividend_is_reversed_not_deleted(earned, two_shareholders):
    m1, _ = two_shareholders
    officer = _officer(earned, "sec@x.co")
    d = post_dividend(_declare(earned))

    resp = _client(officer, earned).delete(f"/api/v1/dividends/{d.id}/")
    assert resp.status_code == 400

    resp = _client(officer, earned).post(
        f"/api/v1/dividends/{d.id}/reverse/",
        {"reason": "Declared on the wrong figures"}, format="json")
    assert resp.status_code == 200, resp.content
    d.refresh_from_db()
    assert d.status == DividendDeclaration.Status.REVERSED
    assert d.reversal_journal is not None
    assert member_balance(m1) == Decimal("0.00")
    assert distributable_surplus(earned)["available"] == Decimal("50000.00")
    assert trial_balance(earned)["balanced"] is True


def test_reversal_is_refused_once_a_member_has_withdrawn_it(earned,
                                                            two_shareholders):
    m1, _ = two_shareholders
    d = post_dividend(_declare(earned))
    with use_tenant(earned):
        funds = Account.all_objects.get(cooperative=earned, code="2000")
        cash = Account.all_objects.get(cooperative=earned, code="1000")
        post_journal(cooperative=earned, reference="WD-1", memo="withdrawal",
                     lines=[Line(account=funds, debit=Decimal("6000"),
                                 membership=m1),
                            Line(account=cash, credit=Decimal("6000"))])

    with pytest.raises(DividendError, match="withdrawn"):
        reverse_dividend(d, reason="Wrong figures")


def test_a_reversed_period_can_be_declared_again(earned, two_shareholders):
    reverse_dividend(post_dividend(_declare(earned)), reason="Wrong figures")
    assert _declare(earned).status == DividendDeclaration.Status.DRAFT


# ── Who may do what ─────────────────────────────────────────────────────────
def test_an_ordinary_member_cannot_declare_or_see_every_allocation(
        earned, two_shareholders):
    m1, _ = two_shareholders
    client = _client(m1.user, earned)

    assert client.post("/api/v1/dividends/",
                       {"total_amount": "1000", "period_label": "X"},
                       format="json").status_code == 403
    assert client.get("/api/v1/dividends/").status_code == 403


def test_members_see_only_posted_dividends(earned, two_shareholders):
    m1, _ = two_shareholders
    _declare(earned, period_label="FY2024")
    post_dividend(_declare(earned, period_label="FY2025"))

    rows = _client(m1.user, earned).get("/api/v1/me/dividends/").json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert [r["period_label"] for r in rows] == ["FY2025"]


def test_officers_see_the_surplus_and_declare_by_rate(earned,
                                                      two_shareholders):
    client = _client(_officer(earned, "sec@x.co"), earned)

    surplus = client.get("/api/v1/dividends/surplus/").json()
    assert surplus["available"] == "50000.00"
    assert surplus["eligible_members"] == 2

    resp = client.post("/api/v1/dividends/",
                       {"rate": "5", "period_label": "FY2026"}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["total_amount"] == "5000.00"
