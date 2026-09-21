"""Phase 2: PDF ASCII/Naira fix + CSV exports."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from contributions.services import record_contribution
from core.context import use_tenant
from reports.pdf import _ascii, _money, member_statement_pdf

pytestmark = pytest.mark.django_db


def _admin(coop, email="sec@imole.coop"):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name="Sec", password="x")
        Membership.objects.create(user=u, member_no="ADM-1",
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


# ── PDF ─────────────────────────────────────────────────────────────────────
def test_money_uses_N_not_naira_sign():
    assert _money(35000) == "N 35,000.00"
    assert "₦" not in _money(35000)


def test_ascii_transliterates_non_latin1():
    assert _ascii("Ìmọ̀lè Multipurpose CS") == "Imole Multipurpose CS"
    assert _ascii("₦5,000") == "5,000"  # the sign is dropped, digits remain


def test_statement_pdf_generates(coop, member):
    pdf = member_statement_pdf(member)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 500


# ── CSV exports ─────────────────────────────────────────────────────────────
def test_ledger_and_contributions_csv(coop, member, dues_type):
    admin = _admin(coop)
    with use_tenant(coop):
        record_contribution(cooperative=coop, membership=member,
                            contribution_type=dues_type, amount="5000",
                            channel="cash")
    client = _client(admin)

    r = client.get("/api/v1/ledger-entries/export/")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert r.content.decode().splitlines()[0].startswith("date,reference,account_code")
    assert len(r.content.decode().splitlines()) > 1  # has entries

    r = client.get("/api/v1/contributions/export/")
    assert r.status_code == 200
    assert r.content.decode().splitlines()[0].startswith("date,member_no,type,amount")


def test_reports_csv(coop, member, dues_type):
    admin = _admin(coop)
    client = _client(admin)
    r = client.get("/api/v1/reports/arrears-csv/")
    assert r.status_code == 200
    assert r.content.decode().splitlines()[0] == "member_no,expected,collected,outstanding"

    r = client.get("/api/v1/reports/contribution-summary-csv/")
    assert r.status_code == 200
    assert r.content.decode().splitlines()[0] == "type,total,count"
