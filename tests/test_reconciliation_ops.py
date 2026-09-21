"""Phase 3: bulk rematch-all reconciliation action."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from payments.models import PaymentEvent

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


def test_rematch_all_runs_over_exceptions(coop):
    admin = _admin(coop)
    with use_tenant(coop):
        PaymentEvent.objects.create(
            cooperative=coop, provider="paystack", event_id="evt_1",
            reference="PSTK_UNMATCHED", amount=Decimal("5000"),
            status=PaymentEvent.Status.UNMATCHED)

    resp = _client(admin).post("/api/v1/payment-events/rematch-all/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["rematched"] >= 1
    assert "summary" in body
