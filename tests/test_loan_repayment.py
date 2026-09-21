"""Loan repayment schedule, member online repayment, and notifications."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from communications.models import Notification
from core.context import use_tenant
from loans.models import Loan, LoanProduct, LoanRepayment, RepaymentInstalment
from loans.services import approve_loan, disburse_loan, record_repayment
from reports.financials import trial_balance

pytestmark = pytest.mark.django_db


def _officer(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="officer@x.co", full_name="Officer",
                                     password="x")
        return Membership.objects.create(
            user=u, member_no="OFF-1",
            role=Role.objects.filter(slug="secretary").first())


def _client(membership):
    token, _ = Token.objects.get_or_create(user=membership.user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


def _disbursed_loan(coop, member, product, principal="100000", term=4):
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal(principal),
            interest_rate=Decimal("10"), term_months=term)
        approve_loan(loan, approve=True)
        disburse_loan(loan)
        loan.refresh_from_db()
        return loan


def test_schedule_built_on_disbursement(coop, member, product):
    loan = _disbursed_loan(coop, member, product, "100000", term=4)
    with use_tenant(coop):
        rows = list(RepaymentInstalment.objects.filter(loan=loan)
                    .order_by("sequence"))
    assert len(rows) == 4
    # Flat 10% of 100,000 → 10,000 interest → total 110,000; every instalment
    # is the same fixed amount (27,500).
    assert sum(r.amount_due for r in rows) == Decimal("110000.00")
    assert rows[0].amount_due == Decimal("27500.00")
    assert rows[0].principal_component == Decimal("25000.00")
    assert rows[0].interest_component == Decimal("2500.00")
    # Each row's split sums to its payment.
    assert all(r.principal_component + r.interest_component == r.amount_due
               for r in rows)
    assert [r.sequence for r in rows] == [1, 2, 3, 4]
    assert rows[0].due_date < rows[-1].due_date


def test_repayment_allocates_to_earliest_instalment(coop, member, product):
    loan = _disbursed_loan(coop, member, product, "100000", term=4)
    # Fixed instalment is 27,500. Pay exactly one instalment.
    with use_tenant(coop):
        record_repayment(loan, amount="27500")
        rows = list(RepaymentInstalment.objects.filter(loan=loan)
                    .order_by("sequence"))
    assert rows[0].status == RepaymentInstalment.Status.PAID
    assert rows[1].status == RepaymentInstalment.Status.PENDING
    assert trial_balance(coop)["balanced"] is True


def test_disbursement_notifies_member(coop, member, product):
    _disbursed_loan(coop, member, product)
    with use_tenant(coop):
        note = Notification.all_objects.filter(
            membership=member, kind=Notification.Kind.LOAN).first()
    assert note is not None
    assert "disbursed" in note.title.lower()


def test_member_repays_online_end_to_end(coop, member, product):
    loan = _disbursed_loan(coop, member, product, "100000", term=4)
    client = _client(member)

    init = client.post(f"/api/v1/me/loans/{loan.id}/repay-initiate/",
                       {"amount": "27500"}, format="json")
    assert init.status_code == 201, init.content
    reference = init.json()["reference"]
    # Dev placeholder key → simulated flow.
    assert init.json()["payment_simulated"] is True
    with use_tenant(coop):
        assert LoanRepayment.all_objects.filter(
            loan=loan, status=LoanRepayment.Status.PENDING).count() == 1

    verify = client.post(f"/api/v1/me/loans/{loan.id}/repay-verify/",
                         {"reference": reference}, format="json")
    assert verify.status_code == 200, verify.content
    # 110,000 total − 27,500 first instalment.
    assert Decimal(verify.json()["outstanding"]) == Decimal("82500.00")
    with use_tenant(coop):
        assert LoanRepayment.all_objects.filter(
            loan=loan, status=LoanRepayment.Status.CONFIRMED).count() == 1
        assert LoanRepayment.all_objects.filter(
            loan=loan, status=LoanRepayment.Status.PENDING).count() == 0
        assert trial_balance(coop)["balanced"] is True


def test_member_cannot_repay_another_members_loan(coop, member, make_member,
                                                   product):
    loan = _disbursed_loan(coop, member, product)
    other = make_member()
    resp = _client(other).post(
        f"/api/v1/me/loans/{loan.id}/repay-initiate/",
        {"amount": "1000"}, format="json")
    assert resp.status_code == 404


def test_repayment_cannot_exceed_outstanding(coop, member, product):
    loan = _disbursed_loan(coop, member, product, "100000", term=4)
    resp = _client(member).post(
        f"/api/v1/me/loans/{loan.id}/repay-initiate/",
        {"amount": "999999"}, format="json")
    assert resp.status_code == 400


def test_coop_bank_shown_to_member(coop, member, product):
    with use_tenant(coop):
        coop.bank_name = "Zenith Bank"
        coop.bank_account_name = "Imole Coop"
        coop.bank_account_no = "1234567890"
        coop.save()
    loan = _disbursed_loan(coop, member, product)
    body = _client(member).get(f"/api/v1/me/loans/{loan.id}/").json()
    assert body["coop_bank"]["account_no"] == "1234567890"
    assert body["coop_bank"]["bank_name"] == "Zenith Bank"
    # And the schedule is exposed for the member to follow.
    assert len(body["instalments"]) == loan.term_months


def test_member_reports_transfer_officer_confirms(coop, member, product):
    loan = _disbursed_loan(coop, member, product, "100000", term=4)  # total 110k
    officer = _officer(coop)

    # Member taps "I have paid" after a bank transfer.
    r = _client(member).post(
        f"/api/v1/me/loans/{loan.id}/report-transfer/",
        {"amount": "27500", "note": "GTBank 28 Jul"}, format="json")
    assert r.status_code == 201, r.content

    with use_tenant(coop):
        claim = LoanRepayment.all_objects.get(loan=loan)
        assert claim.status == LoanRepayment.Status.PENDING
        assert claim.channel == LoanRepayment.Channel.TRANSFER
        assert claim.note == "GTBank 28 Jul"
        # The officer is notified in the console.
        assert Notification.all_objects.filter(
            membership=officer, kind=Notification.Kind.LOAN,
            title__icontains="verify").exists()

    # It doesn't touch the balance until confirmed.
    loan.refresh_from_db()
    assert loan.outstanding == Decimal("110000.00")

    # Officer verifies and confirms in the console.
    resp = _client(officer).post(
        f"/api/v1/loan-repayments/{claim.id}/confirm/", format="json")
    assert resp.status_code == 200, resp.content
    loan.refresh_from_db()
    assert loan.outstanding == Decimal("82500.00")
    with use_tenant(coop):
        assert LoanRepayment.all_objects.filter(
            loan=loan, status=LoanRepayment.Status.CONFIRMED).count() == 1
        assert trial_balance(coop)["balanced"] is True


def test_officer_sees_pending_claims_and_can_reject(coop, member, product):
    loan = _disbursed_loan(coop, member, product, "100000", term=4)
    officer = _officer(coop)
    _client(member).post(f"/api/v1/me/loans/{loan.id}/report-transfer/",
                         {"amount": "1000"}, format="json")

    oc = _client(officer)
    pending = oc.get("/api/v1/loan-repayments/?status=pending").json()
    rows = pending["results"] if isinstance(pending, dict) else pending
    assert len(rows) == 1
    assert rows[0]["member_no"] == member.member_no
    claim_id = rows[0]["id"]

    assert oc.post(f"/api/v1/loan-repayments/{claim_id}/reject/",
                   format="json").status_code == 200
    with use_tenant(coop):
        assert not LoanRepayment.all_objects.filter(id=claim_id).exists()
        # The member is told it wasn't confirmed.
        assert Notification.all_objects.filter(
            membership=member, title__icontains="not confirmed").exists()
