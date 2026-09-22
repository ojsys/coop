"""
Receipts, officer alerts and the scheduled reminders.

The through-line: a notification must never be able to undo the money movement
that produced it, and a reminder nobody receives is not a reminder. Every send
here happens after commit, which is why these tests capture commit callbacks.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from accounts.models import Membership, Role, User
from communications.models import Notification
from contributions.services import record_contribution
from core.context import use_tenant
from governance.models import Meeting

pytestmark = pytest.mark.django_db


@pytest.fixture
def officer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(
            email="secretary@imole.coop", full_name="Ngozi Secretary",
            password="x")
        Membership.objects.create(
            user=user, member_no="IMC-0001",
            role=Role.objects.filter(slug="secretary").first())
    return user


# ── Contribution receipts ───────────────────────────────────────────────────
def test_recording_a_contribution_emails_a_receipt(
        coop, member, dues_type, django_capture_on_commit_callbacks):
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=Decimal("5000.00"), channel="cash")

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [member.user.email]
    html = message.alternatives[0][0]
    assert "5,000.00" in html, "the amount must appear on the receipt"
    assert dues_type.name in html
    assert coop.name in html, "receipts carry the society's brand"


def test_a_receipt_failure_cannot_undo_the_contribution(
        coop, member, dues_type, monkeypatch,
        django_capture_on_commit_callbacks):
    """The money is recorded even when the mail server is unreachable.

    Patches the *actual send*, so the guard inside send_branded_email is the
    thing under test. Patching that helper itself would only prove a mock
    raised — and because the receipt fires from an on_commit callback, it must
    run inside capture or nothing is exercised at all.
    """
    from django.core.mail import EmailMultiAlternatives

    def explode(self, *args, **kwargs):
        raise RuntimeError("SMTP is down")

    monkeypatch.setattr(EmailMultiAlternatives, "send", explode)

    with django_capture_on_commit_callbacks(execute=True):
        contribution = record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount=Decimal("2500.00"), channel="cash")

    contribution.refresh_from_db()
    assert contribution.journal_id, "the ledger posting must survive"
    assert contribution.status == contribution.Status.CONFIRMED
    assert mail.outbox == [], "nothing was delivered, and nothing blew up"


# ── Loan repayment receipts ─────────────────────────────────────────────────
def test_loan_repayment_sends_one_receipt_not_two_emails(
        coop, member, django_capture_on_commit_callbacks):
    from loans.models import Loan, LoanProduct
    from loans.services import disburse_loan, record_repayment

    with use_tenant(coop):
        product = LoanProduct.objects.create(
            name="Emergency Loan", interest_rate=Decimal("10.00"),
            max_amount=Decimal("500000.00"))
        loan = Loan.objects.create(
            membership=member, product=product,
            principal=Decimal("120000.00"), interest_rate=Decimal("10.00"),
            term_months=6, status=Loan.Status.APPROVED)
        disburse_loan(loan)

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        with use_tenant(coop):
            record_repayment(loan, amount=Decimal("22000.00"))

    # One email: the receipt. The in-app notice suppresses its plain duplicate.
    assert len(mail.outbox) == 1
    html = mail.outbox[0].alternatives[0][0]
    assert "22,000.00" in html
    assert "Loan repayment received" in html

    with use_tenant(coop):
        assert Notification.objects.filter(
            membership=member, kind=Notification.Kind.LOAN).exists()


# ── Arrears reminders now reach beyond the app ──────────────────────────────
def test_arrears_reminders_email_as_well_as_notify(coop, member, dues_type):
    from reports.services import send_arrears_reminders

    mail.outbox.clear()
    result = send_arrears_reminders(coop)

    assert result["reached"] >= 1
    assert mail.outbox, "an in-app-only reminder reaches nobody who isn't looking"
    assert member.user.email in [to for m in mail.outbox for to in m.to]


# ── Meeting reminders ───────────────────────────────────────────────────────
def test_meeting_reminders_cover_only_the_window(coop, member):
    from governance.reminders import send_meeting_reminders

    with use_tenant(coop):
        Meeting.objects.create(
            title="AGM", scheduled_at=timezone.now() + timezone.timedelta(days=1))
        Meeting.objects.create(
            title="Far-off meeting",
            scheduled_at=timezone.now() + timezone.timedelta(days=30))

    result = send_meeting_reminders(coop, days_ahead=2)

    assert result["meetings"] == 1, "only the imminent meeting is worth a nudge"
    with use_tenant(coop):
        notes = Notification.objects.filter(kind=Notification.Kind.MEETING)
        assert notes.exists()
        assert "AGM" in notes.first().title


def test_meeting_reminders_ignore_concluded_meetings(coop, member):
    from governance.reminders import send_meeting_reminders

    with use_tenant(coop):
        Meeting.objects.create(
            title="Already done", status=Meeting.Status.CONCLUDED,
            scheduled_at=timezone.now() + timezone.timedelta(hours=6))

    assert send_meeting_reminders(coop, days_ahead=2)["meetings"] == 0


# ── The cron entry point ────────────────────────────────────────────────────
def test_dry_run_sends_nothing(coop, member, dues_type):
    mail.outbox.clear()
    call_command("send_reminders", "--dry-run")
    assert mail.outbox == [], "--dry-run must not contact anybody"


def test_command_sends_arrears_reminders(coop, member, dues_type):
    mail.outbox.clear()
    call_command("send_reminders", "--skip-meetings")
    assert mail.outbox, "the scheduled run should reach members in arrears"


def test_command_skips_inactive_societies(coop, member, dues_type):
    from tenants.models import Cooperative

    Cooperative.objects.filter(pk=coop.pk).update(
        status=Cooperative.Status.SUSPENDED)
    mail.outbox.clear()
    call_command("send_reminders")
    assert mail.outbox == [], "a suspended society should not message members"


# ── Officer alerts ──────────────────────────────────────────────────────────
def test_approval_request_alerts_officers_by_email(
        coop, member, officer, django_capture_on_commit_callbacks):
    from approvals.models import ApprovalRequest
    from approvals.services import submit_request
    from loans.models import Loan, LoanProduct

    with use_tenant(coop):
        product = LoanProduct.objects.create(name="Emergency",
                                             max_amount=Decimal("100000.00"))
        loan = Loan.objects.create(membership=member, product=product,
                                   principal=Decimal("50000.00"))

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        with use_tenant(coop):
            submit_request(cooperative=coop,
                           action=ApprovalRequest.Action.LOAN_APPROVE,
                           object_id=loan.id, requested_by=member.user)

    recipients = [to for m in mail.outbox for to in m.to]
    assert officer.email in recipients, "the checker must know they're needed"
    assert member.user.email not in recipients, "the maker is not the checker"
