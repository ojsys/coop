"""Dashboards, arrears, contribution summary, PDF statements (PRD §6.5)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from contributions.services import record_contribution
from core.context import use_tenant
from reports import services
from reports.pdf import member_statement_pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def with_activity(coop, member, make_member, dues_type):
    """member pays dues; a second member does not (→ arrears)."""
    laggard = make_member()  # active, pays nothing
    with use_tenant(coop):
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="5000",
                            channel="cash")
    return {"payer": member, "laggard": laggard}


def test_dashboard_aggregates(coop, with_activity):
    with use_tenant(coop):
        data = services.dashboard(coop)
    assert data["contributions_this_period"] == Decimal("5000.00")
    assert data["total_savings"] == Decimal("5000.00")
    assert data["active_members"] == 2
    assert data["members_in_arrears"] == 1
    assert data["arrears_outstanding"] == Decimal("5000.00")
    assert len(data["contributions_trend"]) == 6
    assert "reconciliation" in data


def test_arrears_report(coop, with_activity):
    now = timezone.now()
    with use_tenant(coop):
        report = services.arrears_report(coop, year=now.year, month=now.month)
    assert report["members_in_arrears"] == 1
    assert report["total_outstanding"] == Decimal("5000.00")
    assert report["items"][0]["outstanding"] == Decimal("5000.00")


def test_contribution_summary(coop, with_activity):
    now = timezone.now()
    with use_tenant(coop):
        summary = services.contribution_summary(
            coop, year=now.year, month=now.month,
        )
    assert summary["total"] == Decimal("5000.00")
    assert summary["by_type"][0]["type"] == "Monthly Dues"
    assert summary["by_type"][0]["count"] == 1


def test_member_statement_pdf(coop, member, dues_type):
    with use_tenant(coop):
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="5000",
                            channel="cash")
        pdf = member_statement_pdf(member)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500
