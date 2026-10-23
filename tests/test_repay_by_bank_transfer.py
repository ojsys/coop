"""
Repaying by direct bank transfer, with a 30-minute window.

Paystack takes a fee on every repayment, which the member pays on top. A
transfer straight to the society's own account costs nothing, so the repayment
screen offers it: the member picks an amount, is shown the society's account,
the amount and a short reference for the narration, and has 30 minutes to
send it. Tapping "I've sent it" is what puts it in front of an officer.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from communications.models import Notification
from core.context import use_tenant
from loans.models import LoanRepayment
from tests.test_loan_repayment import _client, _disbursed_loan, _officer
from tests.test_loan_repayment import product  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


@pytest.fixture
def banked_coop(coop):
    coop.bank_name = "Zenith Bank"
    coop.bank_account_name = "Imole Coop"
    coop.bank_account_no = "1234567890"
    coop.save()
    return coop


def _start(member, loan, amount="27500"):
    return _client(member).post(
        f"/api/v1/me/loans/{loan.id}/start-transfer/", {"amount": amount},
        format="json")


def _pending(officer):
    body = _client(officer).get("/api/v1/loan-repayments/?status=pending").json()
    return body["results"] if isinstance(body, dict) else body


def test_starting_shows_the_account_amount_reference_and_window(
        banked_coop, member, product):
    loan = _disbursed_loan(banked_coop, member, product)
    before = timezone.now()

    resp = _start(member, loan)

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["bank"] == {"bank_name": "Zenith Bank",
                            "account_name": "Imole Coop",
                            "account_no": "1234567890"}
    assert body["amount"] == "27500.00"
    assert body["psp_reference"].startswith(f"L{loan.id}-")
    window = timezone.datetime.fromisoformat(
        body["expires_at"].replace("Z", "+00:00")) - before
    assert timedelta(minutes=29) < window <= timedelta(minutes=30, seconds=5)


def test_a_started_transfer_is_not_an_officer_claim_yet(banked_coop, member,
                                                        product):
    """Nothing has been sent, so there is nothing for an officer to find."""
    loan = _disbursed_loan(banked_coop, member, product)
    officer = _officer(banked_coop)

    _start(member, loan)

    assert _pending(officer) == []
    with use_tenant(banked_coop):
        assert not Notification.all_objects.filter(
            membership=officer, title__icontains="verify").exists()
    loan.refresh_from_db()
    assert loan.outstanding == Decimal("110000.00")


def test_ive_sent_it_reaches_the_officer_with_the_reference(banked_coop,
                                                            member, product):
    loan = _disbursed_loan(banked_coop, member, product)
    officer = _officer(banked_coop)
    started = _start(member, loan).json()

    resp = _client(member).post(
        f"/api/v1/me/loans/{loan.id}/report-transfer/",
        {"transfer": started["id"]}, format="json")

    assert resp.status_code == 201, resp.content
    assert resp.json()["expires_at"] is None
    rows = _pending(officer)
    assert [r["psp_reference"] for r in rows] == [started["psp_reference"]]
    assert rows[0]["amount"] == "27500.00"
    with use_tenant(banked_coop):
        note = Notification.all_objects.get(membership=officer,
                                            title__icontains="verify")
    assert started["psp_reference"] in note.body

    confirm = _client(officer).post(
        f"/api/v1/loan-repayments/{rows[0]['id']}/confirm/")
    assert confirm.status_code == 200, confirm.content
    loan.refresh_from_db()
    assert loan.outstanding == Decimal("82500.00")


def test_sent_after_the_window_is_still_accepted(banked_coop, member, product):
    """The 30 minutes guide the member; money that arrived late is still theirs."""
    loan = _disbursed_loan(banked_coop, member, product)
    started = _start(member, loan).json()
    LoanRepayment.all_objects.filter(pk=started["id"]).update(
        expires_at=timezone.now() - timedelta(minutes=5))

    resp = _client(member).post(
        f"/api/v1/me/loans/{loan.id}/report-transfer/",
        {"transfer": started["id"]}, format="json")

    assert resp.status_code == 201, resp.content
    assert len(_pending(_officer(banked_coop))) == 1


def test_starting_again_replaces_the_unsent_one(banked_coop, member, product):
    loan = _disbursed_loan(banked_coop, member, product)
    _start(member, loan, "1000")
    _start(member, loan, "2000")

    rows = LoanRepayment.all_objects.filter(loan=loan)
    assert [r.amount for r in rows] == [Decimal("2000.00")]


def test_a_sent_transfer_is_not_replaced(banked_coop, member, product):
    loan = _disbursed_loan(banked_coop, member, product)
    first = _start(member, loan, "1000").json()
    _client(member).post(f"/api/v1/me/loans/{loan.id}/report-transfer/",
                         {"transfer": first["id"]}, format="json")

    _start(member, loan, "2000")

    assert LoanRepayment.all_objects.filter(loan=loan).count() == 2


def test_cancel_drops_an_unsent_transfer(banked_coop, member, product):
    loan = _disbursed_loan(banked_coop, member, product)
    started = _start(member, loan).json()

    resp = _client(member).post(f"/api/v1/me/loans/{loan.id}/cancel-transfer/",
                                {"transfer": started["id"]}, format="json")

    assert resp.status_code == 204
    assert not LoanRepayment.all_objects.filter(loan=loan).exists()


def test_cancel_refuses_a_transfer_already_reported(banked_coop, member,
                                                    product):
    loan = _disbursed_loan(banked_coop, member, product)
    started = _start(member, loan).json()
    _client(member).post(f"/api/v1/me/loans/{loan.id}/report-transfer/",
                         {"transfer": started["id"]}, format="json")

    resp = _client(member).post(f"/api/v1/me/loans/{loan.id}/cancel-transfer/",
                                {"transfer": started["id"]}, format="json")

    assert resp.status_code == 400
    assert LoanRepayment.all_objects.filter(loan=loan).count() == 1


def test_no_society_account_means_no_transfer_option(coop, member, product):
    loan = _disbursed_loan(coop, member, product)

    resp = _start(member, loan)

    assert resp.status_code == 400
    assert "Paystack" in str(resp.json())


def test_more_than_is_owed_is_refused(banked_coop, member, product):
    loan = _disbursed_loan(banked_coop, member, product)

    assert _start(member, loan, "999999").status_code == 400
