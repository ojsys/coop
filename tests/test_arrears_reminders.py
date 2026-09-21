"""Phase 12: arrears reminders to members behind on mandatory dues."""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from communications.models import Notification
from core.context import use_tenant
from reports.services import send_arrears_reminders

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


def test_reminders_notify_members_in_arrears(coop, member, dues_type):
    # `member` owes mandatory dues (dues_type) but has not paid → in arrears.
    result = send_arrears_reminders(coop)
    assert result["members_in_arrears"] >= 1
    assert result["reached"] == result["members_in_arrears"]
    with use_tenant(coop):
        notes = Notification.objects.filter(membership=member,
                                            kind=Notification.Kind.CONTRIBUTION)
        assert notes.exists()
        assert "outstanding" in notes.first().body.lower()


def test_reminder_endpoint(coop, member, dues_type):
    resp = _client(_admin(coop)).post("/api/v1/reports/send-arrears-reminders/")
    assert resp.status_code == 200
    assert "reached" in resp.json()
