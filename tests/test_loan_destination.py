"""
Where a loan gets paid is frozen when it is applied for.

A member can edit their own bank details. Without a snapshot, changing the
account number after an officer approved a loan would silently redirect the
payout — the officer would have vetted one destination and a different one
would be paid. So the destination is captured on the loan at application and
frozen from APPROVED onward.

The blank case is the subtle one. Locking bank edits outright would trap a
member who applied before supplying an account: snapshot blank, edits refused,
loan unpayable for ever. So an empty field can still be *filled* — and while a
loan is PENDING its snapshot follows that fill. Only a *change* to an existing
value is refused, and only while a loan is live.

Cash disbursement needs no account at all (``disburse_loan`` defaults to the
Cash account), so a blank destination is not an error here. It is refused at
electronic disbursement, where it matters.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from loans.models import Loan, LoanProduct

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _officer(coop, email="sec@imole.coop", slug=Role.SECRETARY):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email,
                                        password="x")
        Membership.objects.create(
            user=user, member_no=email[:6],
            role=Role.objects.filter(slug=slug).first())
    return user


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def banked(coop, member):
    """A member with a complete payout destination."""
    member.bank_name = "Access Bank"
    member.bank_code = "044"
    member.bank_account_no = "0123456789"
    member.save(update_fields=["bank_name", "bank_code", "bank_account_no"])
    return member


# ── The snapshot ────────────────────────────────────────────────────────────
def test_applying_captures_the_destination(coop, banked, product):
    resp = _client(banked.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "50000", "term_months": 6},
        format="json", **_h(coop))

    assert resp.status_code == 201, resp.content
    loan = Loan.all_objects.get()
    assert loan.destination_bank_name == "Access Bank"
    assert loan.destination_bank_code == "044"
    assert loan.destination_account_no == "0123456789"


def test_an_officer_applying_on_behalf_captures_it_too(coop, banked, product):
    """Both paths come through LoanSerializer.create, so neither can forget."""
    officer = _officer(coop)

    resp = _client(officer).post(
        "/api/v1/loans/",
        {"membership": banked.id, "product": product.id, "principal": "50000"},
        format="json", **_h(coop))

    assert resp.status_code == 201, resp.content
    assert Loan.all_objects.get().destination_bank_code == "044"


def test_a_client_cannot_name_its_own_destination(coop, banked, product):
    """Otherwise an applicant could choose the account their loan is paid to."""
    resp = _client(banked.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "50000",
         "destination_bank_name": "Attacker Bank",
         "destination_bank_code": "999",
         "destination_account_no": "9999999999"},
        format="json", **_h(coop))

    assert resp.status_code == 201, resp.content
    loan = Loan.all_objects.get()
    assert loan.destination_account_no == "0123456789"
    assert loan.destination_bank_code == "044"


def test_a_member_with_no_account_may_still_apply(coop, member, product):
    """Cash disbursement needs no bank account, so this is not an error."""
    resp = _client(member.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "50000"},
        format="json", **_h(coop))

    assert resp.status_code == 201, resp.content
    loan = Loan.all_objects.get()
    assert loan.destination_account_no == ""
    assert loan.destination_is_payable is False


# ── The lock ────────────────────────────────────────────────────────────────
def test_a_member_cannot_change_their_account_with_a_live_loan(coop, banked,
                                                               product):
    """The whole point: approve a loan, edit the account, money goes elsewhere."""
    with use_tenant(coop):
        Loan.objects.create(membership=banked, product=product,
                            principal=Decimal("50000"),
                            status=Loan.Status.APPROVED)

    resp = _client(banked.user).patch(
        "/api/v1/me/profile/", {"bank_account_no": "9999999999"},
        format="json", **_h(coop))

    assert resp.status_code == 400, resp.content
    banked.refresh_from_db()
    assert banked.bank_account_no == "0123456789"


def test_the_refusal_explains_itself(coop, banked, product):
    with use_tenant(coop):
        Loan.objects.create(membership=banked, product=product,
                            principal=Decimal("50000"),
                            status=Loan.Status.APPROVED)

    resp = _client(banked.user).patch(
        "/api/v1/me/profile/", {"bank_code": "058"}, format="json", **_h(coop))

    message = str(resp.json())
    assert "loan" in message.lower()
    assert "officer" in message.lower(), "it must say who can help"


@pytest.mark.parametrize("status", ["pending", "approved", "disbursed"])
def test_every_live_status_blocks_a_change(coop, banked, product, status):
    with use_tenant(coop):
        Loan.objects.create(membership=banked, product=product,
                            principal=Decimal("50000"), status=status)

    resp = _client(banked.user).patch(
        "/api/v1/me/profile/", {"bank_account_no": "9999999999"},
        format="json", **_h(coop))

    assert resp.status_code == 400


@pytest.mark.parametrize("status", ["rejected", "repaid"])
def test_a_settled_loan_does_not_block_a_change(coop, banked, product, status):
    """Nothing is owed and nothing is pending, so there is no payout to redirect."""
    with use_tenant(coop):
        Loan.objects.create(membership=banked, product=product,
                            principal=Decimal("50000"), status=status)

    resp = _client(banked.user).patch(
        "/api/v1/me/profile/", {"bank_account_no": "9999999999"},
        format="json", **_h(coop))

    assert resp.status_code == 200, resp.content
    banked.refresh_from_db()
    assert banked.bank_account_no == "9999999999"


def test_a_member_with_no_live_loan_can_change_freely(coop, banked):
    resp = _client(banked.user).patch(
        "/api/v1/me/profile/", {"bank_account_no": "9999999999"},
        format="json", **_h(coop))

    assert resp.status_code == 200, resp.content


# ── Filling a blank is not a redirect ───────────────────────────────────────
def test_filling_a_blank_account_is_allowed_with_a_live_loan(coop, member,
                                                             product):
    """Otherwise a member who applied before adding details is trapped: blank
    snapshot, edits refused, loan unpayable for ever."""
    with use_tenant(coop):
        Loan.objects.create(membership=member, product=product,
                            principal=Decimal("50000"),
                            status=Loan.Status.PENDING)

    resp = _client(member.user).patch(
        "/api/v1/me/profile/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", **_h(coop))

    assert resp.status_code == 200, resp.content
    member.refresh_from_db()
    assert member.bank_account_no == "0123456789"


def test_filling_a_blank_refreshes_a_pending_loans_snapshot(coop, member,
                                                            product):
    with use_tenant(coop):
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("50000"),
                                   status=Loan.Status.PENDING)
    assert loan.destination_account_no == ""

    _client(member.user).patch(
        "/api/v1/me/profile/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", **_h(coop))

    loan.refresh_from_db()
    assert loan.destination_bank_code == "044"
    assert loan.destination_account_no == "0123456789"
    assert loan.destination_is_payable is True


def test_an_approved_loans_snapshot_is_never_refreshed(coop, member, product):
    """The freeze is the control. An approved payout must not move."""
    with use_tenant(coop):
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("50000"),
                                   status=Loan.Status.APPROVED)

    _client(member.user).patch(
        "/api/v1/me/profile/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", **_h(coop))

    loan.refresh_from_db()
    assert loan.destination_account_no == "", (
        "an approved loan's destination must stay as it was vetted")


# ── An officer can still correct a mistake ──────────────────────────────────
def test_an_officer_can_correct_details_for_a_live_loan(coop, banked, product):
    """A mistyped account number has to be fixable. Officer writes to /members/
    are privileged and audited."""
    with use_tenant(coop):
        Loan.objects.create(membership=banked, product=product,
                            principal=Decimal("50000"),
                            status=Loan.Status.APPROVED)
    officer = _officer(coop)

    resp = _client(officer).patch(
        f"/api/v1/members/{banked.id}/", {"bank_account_no": "0123456780"},
        format="json", **_h(coop))

    assert resp.status_code == 200, resp.content
    banked.refresh_from_db()
    assert banked.bank_account_no == "0123456780"


# ── What the officer reads at approval ──────────────────────────────────────
def test_the_summary_names_the_bank_and_code(coop, banked, product):
    with use_tenant(coop):
        loan = Loan.objects.create(membership=banked, product=product,
                                   principal=Decimal("50000"))
    loan.snapshot_destination()

    assert "Access Bank (044)" in loan.describe_destination()
    assert "0123456789" in loan.describe_destination()


def test_the_summary_is_explicit_when_there_is_no_account(coop, member,
                                                          product):
    with use_tenant(coop):
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("50000"))

    summary = loan.describe_destination()
    assert "No account on file" in summary
    assert "cash" in summary.lower()


def test_a_bank_name_without_a_code_is_not_payable(coop, member, product):
    """Every record written before bank selection is in this state: a transfer
    recipient needs the code, so a name alone cannot be paid."""
    member.bank_name = "Access Bank"
    member.bank_account_no = "0123456789"
    member.save(update_fields=["bank_name", "bank_account_no"])
    with use_tenant(coop):
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("50000"))
    loan.snapshot_destination()

    assert loan.destination_is_payable is False
    assert "Access Bank" in loan.describe_destination()


def test_the_api_exposes_the_destination_to_officers(coop, banked, product):
    officer = _officer(coop)
    _client(banked.user).post(
        "/api/v1/me/loans/",
        {"product": product.id, "principal": "50000"},
        format="json", **_h(coop))

    rows = _client(officer).get("/api/v1/loans/", **_h(coop)).json()
    results = rows["results"] if isinstance(rows, dict) else rows

    assert results[0]["destination_account_no"] == "0123456789"
    assert results[0]["destination_is_payable"] is True
    assert "Access Bank (044)" in results[0]["destination_summary"]
