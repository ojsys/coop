"""Phase 10: loan lifecycle — apply, approve, disburse, repay (ledger-posting)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from loans.models import Loan, LoanProduct
from loans.services import approve_loan, disburse_loan, record_repayment
from reports.financials import trial_balance

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
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(name="Quick Loan", interest_rate=Decimal("10"),
                                          max_amount=Decimal("500000"), max_term_months=12)


def test_loan_lifecycle_and_ledger(coop, member, product):
    with use_tenant(coop):
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("100000"),
                                   interest_rate=Decimal("10"), term_months=6)
        # Flat 10% of the amount borrowed → interest 10,000 (term-independent).
        assert loan.total_repayable == Decimal("110000.00")
        assert loan.monthly_instalment == Decimal("18333.34")

        approve_loan(loan, approve=True)
        assert loan.status == Loan.Status.APPROVED

        disburse_loan(loan)
        loan.refresh_from_db()
        assert loan.status == Loan.Status.DISBURSED
        assert loan.outstanding == Decimal("110000.00")
        # Disbursement books only the principal as a receivable.
        assert trial_balance(coop)["balanced"] is True

        record_repayment(loan, amount="100000")
        loan.refresh_from_db()
        assert loan.outstanding == Decimal("10000.00")
        assert loan.status == Loan.Status.DISBURSED

        record_repayment(loan, amount="10000")
        loan.refresh_from_db()
        assert loan.status == Loan.Status.REPAID
        assert loan.outstanding == Decimal("0.00")
        assert trial_balance(coop)["balanced"] is True


def test_loan_endpoints(coop, member, product):
    client = _client(_admin(coop))
    r = client.post("/api/v1/loans/", {
        "membership": member.id, "product": product.id,
        "principal": "50000", "term_months": 6, "purpose": "stock",
    }, format="json")
    assert r.status_code == 201, r.content
    lid = r.json()["id"]
    assert r.json()["status"] == "pending"
    # Flat 10% of 50,000 → 5,000 interest → 55,000 repayable.
    assert Decimal(r.json()["total_repayable"]) == Decimal("55000.00")

    assert client.post(f"/api/v1/loans/{lid}/approve/").json()["status"] == "approved"
    assert client.post(f"/api/v1/loans/{lid}/disburse/").json()["status"] == "disbursed"
    repaid = client.post(f"/api/v1/loans/{lid}/repay/", {"amount": "55000"}, format="json")
    assert repaid.json()["status"] == "repaid"


def test_cannot_disburse_unapproved(coop, member, product):
    client = _client(_admin(coop))
    r = client.post("/api/v1/loans/", {
        "membership": member.id, "product": product.id, "principal": "1000",
    }, format="json")
    lid = r.json()["id"]
    assert client.post(f"/api/v1/loans/{lid}/disburse/").status_code == 400
