"""Phase 9: dividend declaration + posting to the ledger."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from dividends.models import DividendDeclaration
from dividends.services import declare_dividend, post_dividend
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _admin(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="sec@imole.coop", full_name="Sec",
                                     password="x")
        Membership.objects.create(user=u, member_no="ADM-1", share_capital="0",
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


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


def test_declare_allocates_pro_rata(coop, two_shareholders):
    m1, m2 = two_shareholders
    from dividends.models import DividendAllocation
    d = declare_dividend(cooperative=coop, total_amount="10000",
                         period_label="FY2025")
    allocs = {a.membership_id: a.amount
              for a in DividendAllocation.all_objects.filter(declaration=d)}
    assert allocs[m1.id] == Decimal("6000.00")   # 60% of share capital
    assert allocs[m2.id] == Decimal("4000.00")   # 40%
    assert sum(allocs.values()) == Decimal("10000.00")
    assert d.status == DividendDeclaration.Status.DRAFT


def test_post_dividend_balances_ledger(coop, two_shareholders):
    d = declare_dividend(cooperative=coop, total_amount="10000",
                         period_label="FY2025")
    post_dividend(d)
    d.refresh_from_db()
    assert d.status == DividendDeclaration.Status.POSTED
    assert d.journal is not None
    # The ledger still balances after the distribution posts.
    assert trial_balance(coop)["balanced"] is True


def test_dividend_endpoints(coop, two_shareholders):
    client = _client(_admin(coop))
    r = client.post("/api/v1/dividends/",
                    {"total_amount": "10000", "period_label": "FY2025"},
                    format="json")
    assert r.status_code == 201, r.content
    did = r.json()["id"]
    assert r.json()["member_count"] == 2

    r = client.post(f"/api/v1/dividends/{did}/post_to_ledger/")
    assert r.status_code == 200
    assert r.json()["status"] == "posted"
