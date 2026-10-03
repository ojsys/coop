"""
Paying member savings back out.

A withdrawal is money leaving the cooperative, so it is deliberately two steps:
one officer records it, a *different* privileged officer approves it, and only
then does anything post to the ledger. The posting is the mirror of a
contribution — debit Member Funds (2000), credit whichever account the money
physically left from.

The cases worth reading are the ones that encode the reasoning:

* requesting posts nothing at all;
* the balance is re-checked **at approval**, because two withdrawals that are
  each affordable alone must not overdraw a member together;
* the destination is a snapshot, so a member editing their bank details later
  cannot rewrite where a payment already went.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core import mail
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from approvals.models import ApprovalRequest
from approvals.services import ApprovalError, decide_request
from contributions.models import Contribution
from contributions.services import record_contribution
from core.context import use_tenant
from ledger.models import LedgerEntry
from ledger.services import member_balance
from savings.models import Withdrawal
from savings.services import WithdrawalError, request_withdrawal

pytestmark = pytest.mark.django_db


@pytest.fixture
def saver(coop, member, dues_type):
    """A member with 10,000 of real, ledger-backed savings."""
    with use_tenant(coop):
        record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=Decimal("10000.00"), channel=Contribution.Channel.CASH,
        )
        member.bank_name = "GTBank"
        member.bank_account_no = "0123456789"
        member.save(update_fields=["bank_name", "bank_account_no"])
    assert member_balance(member) == Decimal("10000.00")
    return member


def _officer(coop, email, slug=Role.SECRETARY):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email,
                                        password="x")
        Membership.objects.create(
            user=user, member_no=email[:6],
            role=Role.objects.filter(slug=slug).first())
    return user


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _ask(coop, saver, amount, actor, channel=Withdrawal.Channel.TRANSFER):
    with use_tenant(coop):
        return request_withdrawal(
            cooperative=coop, membership=saver, amount=amount,
            channel=channel, requested_by=actor)


def _approve(approval, actor):
    return decide_request(approval, actor=actor, approve=True)


# ── Requesting posts nothing ─────────────────────────────────────────────────
def test_requesting_a_withdrawal_moves_no_money(coop, saver):
    maker = _officer(coop, "maker@imole.coop")

    withdrawal, approval = _ask(coop, saver, "4000", maker)

    assert approval.status == ApprovalRequest.Status.PENDING
    assert withdrawal.journal is None
    assert not withdrawal.is_paid
    assert member_balance(saver) == Decimal("10000.00"), (
        "the balance must not move until a second officer approves")


def test_the_approver_sees_who_how_much_and_where(coop, saver):
    """summary is the only text the Approvals page renders."""
    maker = _officer(coop, "maker@imole.coop")

    _, approval = _ask(coop, saver, "4000", maker)

    assert "4,000.00" in approval.summary
    assert "0123456789" in approval.summary


# ── Approval posts it ───────────────────────────────────────────────────────
def test_approval_posts_the_payout_and_reduces_the_balance(coop, saver):
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    withdrawal, approval = _ask(coop, saver, "4000", maker)

    _approve(approval, checker)

    withdrawal.refresh_from_db()
    assert withdrawal.is_paid
    assert withdrawal.journal is not None
    assert member_balance(saver) == Decimal("6000.00")


def test_the_posting_debits_member_funds_and_credits_where_it_was_paid_from(
        coop, saver):
    """The mirror of a contribution: 2000 down, the cash account down."""
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    withdrawal, approval = _ask(coop, saver, "4000", maker,
                                channel=Withdrawal.Channel.TRANSFER)

    _approve(approval, checker)

    withdrawal.refresh_from_db()
    # Read through all_objects, not journal.entries: LedgerEntry is
    # tenant-scoped, so the reverse manager returns nothing with no tenant bound
    # — which is every line of a test that is not inside use_tenant().
    lines = {
        e.account.code: e
        for e in LedgerEntry.all_objects.filter(journal=withdrawal.journal)
    }
    assert lines["2000"].debit == Decimal("4000.00")
    assert lines["2000"].membership_id == saver.pk, (
        "without the membership the payout is missing from their statement")
    assert lines["1010"].credit == Decimal("4000.00")


def test_cash_is_paid_out_of_the_cash_account(coop, saver):
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    withdrawal, approval = _ask(coop, saver, "1000", maker,
                                channel=Withdrawal.Channel.CASH)

    _approve(approval, checker)

    withdrawal.refresh_from_db()
    codes = {
        e.account.code
        for e in LedgerEntry.all_objects.filter(journal=withdrawal.journal)
    }
    assert codes == {"2000", "1000"}


def test_the_maker_cannot_approve_their_own_withdrawal(coop, saver):
    maker = _officer(coop, "maker@imole.coop")
    _, approval = _ask(coop, saver, "4000", maker)

    with pytest.raises(ApprovalError):
        _approve(approval, maker)

    assert member_balance(saver) == Decimal("10000.00")


# ── The balance guard ───────────────────────────────────────────────────────
def test_more_than_the_balance_is_refused_up_front(coop, saver):
    maker = _officer(coop, "maker@imole.coop")

    with pytest.raises(WithdrawalError) as exc:
        _ask(coop, saver, "10000.01", maker)

    assert "holds" in str(exc.value)
    assert Withdrawal.all_objects.count() == 0


def test_two_affordable_withdrawals_cannot_overdraw_together(coop, saver):
    """The reason the balance is re-checked at approval, not only at request.

    6,000 and 6,000 are each affordable against 10,000. Approving both would
    leave the member owed -2,000, which the ledger would happily record.
    """
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    _, first = _ask(coop, saver, "6000", maker)
    _, second = _ask(coop, saver, "6000", maker)

    _approve(first, checker)

    with pytest.raises(WithdrawalError) as exc:
        _approve(second, checker)

    assert "no longer" in str(exc.value)
    assert member_balance(saver) == Decimal("4000.00"), (
        "the member must not be overdrawn")


def test_a_zero_or_negative_amount_is_refused(coop, saver):
    maker = _officer(coop, "maker@imole.coop")

    for bad in ("0", "-500"):
        with pytest.raises(WithdrawalError):
            _ask(coop, saver, bad, maker)


# ── Where the money goes ────────────────────────────────────────────────────
def test_a_transfer_needs_an_account_on_file(coop, member, dues_type):
    """Recording a payout against no destination would leave the ledger saying
    money left with no trace of where to."""
    with use_tenant(coop):
        record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=Decimal("5000.00"), channel=Contribution.Channel.CASH,
        )
    assert not member.bank_account_no
    maker = _officer(coop, "maker@imole.coop")

    with pytest.raises(WithdrawalError) as exc:
        _ask(coop, member, "1000", maker)

    assert "no bank account" in str(exc.value)


def test_cash_does_not_need_an_account_on_file(coop, member, dues_type):
    with use_tenant(coop):
        record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=Decimal("5000.00"), channel=Contribution.Channel.CASH,
        )
    maker = _officer(coop, "maker@imole.coop")

    withdrawal, _ = _ask(coop, member, "1000", maker,
                         channel=Withdrawal.Channel.CASH)

    assert withdrawal.pk is not None


def test_the_destination_is_a_snapshot(coop, saver):
    """A member editing their bank details afterwards must not rewrite the
    record of a payment already made."""
    maker = _officer(coop, "maker@imole.coop")
    withdrawal, _ = _ask(coop, saver, "1000", maker)

    saver.bank_account_no = "9999999999"
    saver.save(update_fields=["bank_account_no"])

    withdrawal.refresh_from_db()
    assert withdrawal.destination_account_no == "0123456789"


# ── Idempotency and notification ────────────────────────────────────────────
def test_paying_twice_posts_one_journal(coop, saver):
    from savings.services import pay_withdrawal

    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    withdrawal, approval = _ask(coop, saver, "4000", maker)
    _approve(approval, checker)
    withdrawal.refresh_from_db()
    journal_id = withdrawal.journal_id

    pay_withdrawal(withdrawal, actor=checker)

    withdrawal.refresh_from_db()
    assert withdrawal.journal_id == journal_id
    assert member_balance(saver) == Decimal("6000.00")


def test_the_member_is_told_their_money_went_out(
        coop, saver, django_capture_on_commit_callbacks):
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    _, approval = _ask(coop, saver, "4000", maker)
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _approve(approval, checker)

    from communications.models import Notification

    note = Notification.all_objects.filter(
        membership=saver, kind=Notification.Kind.WITHDRAWAL).first()
    assert note is not None
    assert "4,000.00" in note.body
    assert "did not request this" in note.body


# ── Who may ask ─────────────────────────────────────────────────────────────
def test_an_ordinary_member_cannot_request_a_payout(coop, saver):
    """Otherwise a member could pay themselves out of their own savings."""
    rank = _officer(coop, "rank@imole.coop", slug=Role.MEMBER)

    resp = _client(rank).post(
        "/api/v1/withdrawals/",
        {"membership": saver.pk, "amount": "1000", "channel": "cash"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 403
    assert Withdrawal.all_objects.count() == 0


def test_an_officer_can_request_one_over_the_api(coop, saver):
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).post(
        "/api/v1/withdrawals/",
        {"membership": saver.pk, "amount": "1000", "channel": "transfer"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 201, resp.content
    assert resp.json()["approval_id"]
    assert member_balance(saver) == Decimal("10000.00")


def test_a_member_may_still_read_their_own_withdrawals(coop, saver):
    rank = _officer(coop, "rank@imole.coop", slug=Role.MEMBER)

    resp = _client(rank).get("/api/v1/withdrawals/",
                             HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200


# ── A member asking for their own savings back (`/me/withdrawals/`) ─────────
#
# The officer endpoint above refuses an ordinary member, which is right: nobody
# should be able to pay *themselves* out. But a member must still be able to
# *ask*. That is what this endpoint is: a request that posts nothing and goes to
# the officers' queue.
#
# The control it carries is deliberately weaker and worth stating plainly: since
# the member is the requester, the maker-checker rule is satisfied by any single
# officer approving — one authorisation, where an officer-initiated payout needs
# two. The member is the beneficiary, so the officer is the check.
def test_a_member_can_request_their_own_savings(coop, saver):
    resp = _client(saver.user).post(
        "/api/v1/me/withdrawals/",
        {"amount": "4000", "channel": "transfer", "reason": "School fees"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 201, resp.content
    assert member_balance(saver) == Decimal("10000.00"), (
        "asking must not move money")
    withdrawal = Withdrawal.all_objects.get()
    assert withdrawal.membership_id == saver.pk
    assert not withdrawal.is_paid


def test_the_member_is_told_nothing_has_been_paid_yet(coop, saver):
    """The screen shows this text; a member who thinks the money is on its way
    will chase the wrong thing."""
    resp = _client(saver.user).post(
        "/api/v1/me/withdrawals/", {"amount": "4000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert "Nothing has been paid out yet" in resp.json()["detail"]
    assert resp.json()["approval_id"]


def test_a_member_cannot_request_against_someone_elses_savings(
        coop, saver, make_member):
    """``membership`` is taken from the caller and never read from the body, so
    naming another member is not refused — it is impossible to express."""
    other = make_member(member_no="M-999")

    resp = _client(saver.user).post(
        "/api/v1/me/withdrawals/",
        {"membership": other.pk, "amount": "1000", "channel": "cash"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 201, resp.content
    withdrawal = Withdrawal.all_objects.get()
    assert withdrawal.membership_id == saver.pk, (
        "the body's membership must be ignored, not honoured")
    assert member_balance(other) == Decimal("0.00")


def test_a_member_cannot_ask_for_more_than_they_hold(coop, saver):
    """The same balance guard as the officer path — one set of rules."""
    resp = _client(saver.user).post(
        "/api/v1/me/withdrawals/", {"amount": "10000.01"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 400, resp.content
    assert "holds" in resp.json()["detail"]
    assert Withdrawal.all_objects.count() == 0


def test_a_single_officer_can_approve_a_member_initiated_request(coop, saver):
    """The documented trade-off, pinned down so it cannot change by accident.

    The member is the maker, so the first officer to approve is already a
    different person and the payout posts. If this ever starts requiring two,
    that is a deliberate policy change and this test should be the thing that
    says so.
    """
    _client(saver.user).post(
        "/api/v1/me/withdrawals/", {"amount": "4000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )
    approval = ApprovalRequest.all_objects.get(
        action=ApprovalRequest.Action.SAVINGS_WITHDRAW)
    officer = _officer(coop, "sec@imole.coop")

    decide_request(approval, actor=officer, approve=True)

    assert member_balance(saver) == Decimal("6000.00")


def test_a_member_cannot_approve_their_own_request(coop, saver):
    """The member is the maker, so they are barred as the checker — otherwise
    this endpoint would be a self-service payout."""
    _client(saver.user).post(
        "/api/v1/me/withdrawals/", {"amount": "4000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )
    approval = ApprovalRequest.all_objects.get(
        action=ApprovalRequest.Action.SAVINGS_WITHDRAW)

    with pytest.raises(ApprovalError):
        decide_request(approval, actor=saver.user, approve=True)

    assert member_balance(saver) == Decimal("10000.00")


def test_a_member_sees_only_their_own_requests(coop, saver, make_member):
    other = make_member(member_no="M-888")
    _client(saver.user).post(
        "/api/v1/me/withdrawals/", {"amount": "1000"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    resp = _client(other.user).get("/api/v1/me/withdrawals/",
                                   HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200
    assert resp.json()["results"] == []
