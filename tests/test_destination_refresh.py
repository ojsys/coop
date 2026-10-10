"""
Keeping a loan's payout destination usable.

A loan's destination is a **snapshot** taken when the member applies. From
APPROVED onward it is frozen, and that freeze is a real control: without it, a
member could edit their account number after an officer vetted the payout and the
money would go somewhere nobody approved.

The freeze had two gaps, and both ended the same way — an officer staring at
"No bank code recorded for this account" with nothing on screen that could fix it.

* **While PENDING the snapshot is supposed to track the member**, but only the
  member's *own* profile edit refreshed it. An officer editing the same fields
  through /members/<id>/, or staff in the Django admin, did not — so the effect of
  an identical change depended on who made it.
* **Once APPROVED nothing could refresh it at all.** A loan approved before the
  member had a bank code kept the blank destination for ever, however many times
  the details were corrected afterwards.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from loans.models import Loan, LoanProduct
from loans.services import (LoanError, refresh_loan_destination,
                            refresh_pending_destinations)

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def _officer(coop, email, slug=Role.SECRETARY):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email,
                                        password="x")
        Membership.objects.create(
            user=user, member_no=email[:6],
            role=Role.all_objects.get(cooperative=coop, slug=slug))
    return user


def _h(coop):
    return {"HTTP_X_COOPERATIVE_ID": str(coop.id)}


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


def _loan(coop, member, product, status):
    """A loan whose destination was captured while the member had no bank code —
    the state every loan approved before bank selection existed is in."""
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6, status=status)
    loan.snapshot_destination()
    loan.save(update_fields=["destination_bank_name", "destination_bank_code",
                             "destination_account_no"])
    assert loan.destination_is_payable is False, "fixture precondition"
    return loan


def _give_bank(member):
    member.bank_name = "Access Bank"
    member.bank_code = "044"
    member.bank_account_no = "0123456789"
    member.save(update_fields=["bank_name", "bank_code", "bank_account_no"])
    return member


# ── A pending loan tracks the member, whoever makes the edit ────────────────
def test_the_members_own_edit_refreshes_a_pending_loan(coop, member, product):
    loan = _loan(coop, member, product, Loan.Status.PENDING)

    resp = _client(member.user).patch(
        "/api/v1/me/profile/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", **_h(coop))

    assert resp.status_code == 200, resp.content
    loan.refresh_from_db()
    assert loan.destination_is_payable is True


def test_an_officers_edit_refreshes_a_pending_loan(coop, member, product):
    """This did nothing before: the same change had different effects depending
    on who made it."""
    loan = _loan(coop, member, product, Loan.Status.PENDING)
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).patch(
        f"/api/v1/members/{member.id}/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", **_h(coop))

    assert resp.status_code == 200, resp.content
    loan.refresh_from_db()
    assert loan.destination_bank_code == "044"
    assert loan.destination_is_payable is True


def test_the_admin_form_refreshes_a_pending_loan(coop, member, product):
    """Staff in the Django admin are the third path, and were the third to miss
    it."""
    from accounts.admin import MembershipAdminForm
    from accounts.models import Role as R

    loan = _loan(coop, member, product, Loan.Status.PENDING)

    with use_tenant(coop):
        form = MembershipAdminForm(instance=member, data={
            "cooperative": member.cooperative_id, "user": member.user_id,
            "member_no": member.member_no, "role": member.role_id or "",
            "kind": member.kind,
            "status": member.status, "full_name": member.user.full_name,
            "phone": member.user.phone,
            "share_capital": str(member.share_capital),
            "joined_at": "", "exited_at": "", "occupation": "",
            "bank_name": "Access Bank", "bank_code": "044",
            "bank_account_no": "0123456789",
            "date_of_birth": "", "gender": "", "address": "",
            "next_of_kin_name": "", "next_of_kin_phone": "",
            "documents-TOTAL_FORMS": "0", "documents-INITIAL_FORMS": "0",
            "documents-MIN_NUM_FORMS": "0", "documents-MAX_NUM_FORMS": "1000",
        })
        form.fields["role"].queryset = R.all_objects.all()
        assert form.is_valid(), form.errors
        form.save()

    loan.refresh_from_db()
    assert loan.destination_is_payable is True


def test_an_approved_loan_is_not_refreshed_automatically(coop, member, product):
    """The control: a vetted payout must not be redirected by a profile edit."""
    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    officer = _officer(coop, "sec2@imole.coop")

    _client(officer).patch(
        f"/api/v1/members/{member.id}/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", **_h(coop))

    loan.refresh_from_db()
    assert loan.destination_bank_code == "", "approval freezes the destination"


# ── An approved loan can be refreshed deliberately ──────────────────────────
def test_an_officer_can_refresh_an_approved_loans_destination(coop, member,
                                                              product):
    """The gap that made this unfixable: approved before the member had a code,
    and nothing afterwards could help."""
    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    _give_bank(member)
    officer = _officer(coop, "sec3@imole.coop")

    resp = _client(officer).post(
        f"/api/v1/loans/{loan.id}/refresh-destination/", **_h(coop))

    assert resp.status_code == 200, resp.content
    loan.refresh_from_db()
    assert loan.destination_bank_code == "044"
    assert loan.destination_account_no == "0123456789"
    assert loan.destination_is_payable is True


def test_refreshing_is_audited_with_both_destinations(coop, member, product):
    """It changes where money will go, so who did it and what changed must be
    recoverable."""
    from audit.models import AuditLog

    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    _give_bank(member)
    officer = _officer(coop, "sec4@imole.coop")

    _client(officer).post(f"/api/v1/loans/{loan.id}/refresh-destination/",
                         **_h(coop))

    entry = AuditLog.all_objects.get(action="loan.refresh_destination")
    assert entry.actor_id == officer.id
    assert entry.before["bank_code"] == ""
    assert entry.after["bank_code"] == "044"


def test_a_refresh_that_changes_nothing_is_not_audited(coop, member, product):
    """Recording "updated" when nothing moved makes the trail less trustworthy,
    not more complete."""
    from audit.models import AuditLog

    _give_bank(member)
    loan = Loan.all_objects.create(
        cooperative=coop, membership=member, product=product,
        principal=Decimal("50000"), interest_rate=Decimal("10"),
        term_months=6, status=Loan.Status.APPROVED,
        destination_bank_name="Access Bank", destination_bank_code="044",
        destination_account_no="0123456789")

    with use_tenant(coop):
        refresh_loan_destination(loan, actor=None)

    assert not AuditLog.all_objects.filter(
        action="loan.refresh_destination").exists()


def test_a_disbursed_loans_destination_cannot_be_changed(coop, member, product):
    """It is the record of where the money actually went."""
    from django.utils import timezone

    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    loan.status = Loan.Status.DISBURSED
    loan.disbursed_at = timezone.now()
    loan.save(update_fields=["status", "disbursed_at"])
    _give_bank(member)

    with use_tenant(coop):
        with pytest.raises(LoanError) as exc:
            refresh_loan_destination(loan)

    assert "where the money actually went" in str(exc.value)


def test_a_member_cannot_refresh_their_own_loans_destination(coop, member,
                                                             product):
    """That would be precisely the redirect the freeze exists to prevent."""
    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    _give_bank(member)

    resp = _client(member.user).post(
        f"/api/v1/loans/{loan.id}/refresh-destination/", **_h(coop))

    assert resp.status_code == 403
    loan.refresh_from_db()
    assert loan.destination_bank_code == ""


def test_a_chairperson_cannot_refresh_a_destination(coop, member, product):
    """Reads are open to office-holders; moving a payout target is not a read."""
    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    _give_bank(member)
    chair = _officer(coop, "chair@imole.coop", Role.CHAIRPERSON)

    resp = _client(chair).post(
        f"/api/v1/loans/{loan.id}/refresh-destination/", **_h(coop))

    assert resp.status_code == 403


# ── The modal has what it needs to offer this ───────────────────────────────
def test_the_loan_exposes_the_members_current_bank_code(coop, member, product):
    """Without it the console cannot tell whether refreshing would help, and
    would offer a button that changes nothing."""
    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    _give_bank(member)
    officer = _officer(coop, "sec5@imole.coop")

    body = _client(officer).get(f"/api/v1/loans/{loan.id}/", **_h(coop)).json()

    assert body["member_bank_code"] == "044"
    assert body["destination_bank_code"] == ""
    assert body["destination_is_payable"] is False


def test_refreshing_then_paying_works_end_to_end(coop, member, product,
                                                 monkeypatch):
    """The whole point: the loan becomes payable."""
    from ledger.models import Account
    from ledger.services import Line, post_journal
    from payments import services

    loan = _loan(coop, member, product, Loan.Status.APPROVED)
    _give_bank(member)
    officer = _officer(coop, "tres@imole.coop", Role.TREASURER)

    with use_tenant(coop):
        wallet, _ = Account.all_objects.get_or_create(
            cooperative=coop, code="1020",
            defaults={"name": "Wallet", "kind": Account.Kind.ASSET,
                      "system": True})
        settle, _ = Account.all_objects.get_or_create(
            cooperative=coop, code="1010",
            defaults={"name": "Settlement", "kind": Account.Kind.ASSET,
                      "system": True})
        post_journal(cooperative=coop, reference="FUND", memo="fund",
                     lines=[Line(account=wallet, debit=Decimal("100000.00")),
                            Line(account=settle, credit=Decimal("100000.00"))])

    class _Stub:
        def create_transfer_recipient(self, **kw):
            return "RCP_1"

        def initiate_transfer(self, **kw):
            return {"status": "pending", "transfer_code": "TRF_1",
                    "raw": {"status": "pending"}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())

    client = _client(officer)
    client.post(f"/api/v1/loans/{loan.id}/refresh-destination/", **_h(coop))
    paid = client.post(f"/api/v1/loans/{loan.id}/pay-electronically/",
                       **_h(coop))

    assert paid.status_code == 200, paid.content
    loan.refresh_from_db()
    assert loan.status == Loan.Status.DISBURSED
