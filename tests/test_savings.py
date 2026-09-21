"""Phase 8: savings products & goals with ledger-derived progress."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from contributions.services import record_contribution
from core.context import use_tenant
from savings.models import SavingsGoal, SavingsProduct

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


def test_product_and_goal_crud(coop, member):
    client = _client(_admin(coop))
    r = client.post("/api/v1/savings-products/",
                    {"name": "Ajo", "interest_rate": "5"}, format="json")
    assert r.status_code == 201, r.content
    pid = r.json()["id"]

    r = client.post("/api/v1/savings-goals/", {
        "membership": member.id, "product": pid, "name": "New shop",
        "target_amount": "100000",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["progress_pct"] == 0


def test_goal_progress_from_contributions(coop, member, dues_type):
    # Product tied to the dues type; confirmed contributions count toward it.
    with use_tenant(coop):
        product = SavingsProduct.objects.create(name="Dues saver",
                                                contribution_type=dues_type)
        goal = SavingsGoal.objects.create(membership=member, product=product,
                                          name="Goal", target_amount=Decimal("10000"))
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="4000",
                            channel="cash")
        assert goal.saved_amount == Decimal("4000")

    client = _client(_admin(coop))
    row = next(g for g in client.get(f"/api/v1/savings-goals/?membership={member.id}").json()["results"]
               if g["id"] == goal.id)
    assert row["progress_pct"] == 40  # 4000 / 10000
