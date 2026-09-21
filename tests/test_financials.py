"""Phase 5: financial statements from the ledger."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from contributions.services import record_contribution
from core.context import use_tenant
from reports import financials

pytestmark = pytest.mark.django_db


def _admin(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="sec@imole.coop", full_name="Sec",
                                     password="x")
        Membership.objects.create(user=u, member_no="ADM-1",
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


@pytest.fixture
def with_contribution(coop, member, dues_type):
    with use_tenant(coop):
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="5000",
                            channel="cash")
    return coop


def test_trial_balance_balances(with_contribution):
    tb = financials.trial_balance(with_contribution)
    assert tb["balanced"] is True
    assert tb["total_debit"] == tb["total_credit"]
    assert tb["total_debit"] == Decimal("5000.00")


def test_balance_sheet_balances(with_contribution):
    bs = financials.balance_sheet(with_contribution)
    # Cash (asset) 5000 = Member Funds (liability) 5000.
    assert bs["assets_total"] == Decimal("5000.00")
    assert bs["balanced"] is True


def test_financial_endpoints(with_contribution):
    client = _client(_admin(with_contribution))
    for path in ["trial-balance", "income-statement", "balance-sheet"]:
        assert client.get(f"/api/v1/reports/{path}/").status_code == 200
    csv = client.get("/api/v1/reports/trial-balance-csv/")
    assert csv.status_code == 200
    assert csv["Content-Type"].startswith("text/csv")
    assert csv.content.decode().splitlines()[0] == "code,name,kind,debit,credit"
