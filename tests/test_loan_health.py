"""
Finding loans whose state does not add up.

The distinction these tests exist to protect: **a backlog is not a defect.**
Before approval began paying loans out, approving one and disbursing it were
separate steps, so every loan approved back then is legitimately APPROVED and
undisbursed. Those are reported as a work queue at `info` severity, with the
reason each cannot be paid automatically. A tool that called them corruption
would "repair" perfectly valid rows.

The other line drawn here: anything that would post or reverse a **ledger entry**
is never corrected automatically. Rebuilding a schedule, rechecking a transfer
and closing a settled loan are safe; deciding what to do about a disbursement
with no journal behind it is a person's job.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.context import use_tenant
from ledger.models import Account, Journal
from ledger.services import Line, post_journal
from loans.health import (
    APPROVED_WITH_SUCCESSFUL_PAYOUT, AWAITING_PAYMENT,
    DISBURSED_WITHOUT_JOURNAL, MISSING_SCHEDULE, PAYOUT_FAILED_STILL_DISBURSED,
    PAYOUT_UNCONFIRMED, SETTLED_NOT_MARKED_REPAID, fix_finding, loan_health,
)
from loans.models import Loan, LoanProduct, RepaymentInstalment
from loans.services import disburse_loan
from payments.models import Payout

pytestmark = pytest.mark.django_db


def _acct(coop, code, name, kind):
    with use_tenant(coop):
        a, _ = Account.all_objects.get_or_create(
            cooperative=coop, code=code,
            defaults={"name": name, "kind": kind, "system": True})
    return a


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick Loan", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def banked(coop, member):
    member.bank_name = "Access Bank"
    member.bank_code = "044"
    member.bank_account_no = "0123456789"
    member.save(update_fields=["bank_name", "bank_code", "bank_account_no"])
    return member


@pytest.fixture
def funded_wallet(coop):
    wallet = _acct(coop, "1020", "Disbursement Wallet", Account.Kind.ASSET)
    settle = _acct(coop, "1010", "Bank / PSP Settlement", Account.Kind.ASSET)
    with use_tenant(coop):
        post_journal(cooperative=coop, reference="TEST-FUND", memo="fund",
                    lines=[Line(account=wallet, debit=Decimal("500000.00")),
                           Line(account=settle, credit=Decimal("500000.00"))])
    return coop


def _loan(coop, member, product, **kw):
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product,
            principal=Decimal("100000"), interest_rate=Decimal("10"),
            term_months=6, **kw)
    loan.snapshot_destination()
    loan.save(update_fields=["destination_bank_name", "destination_bank_code",
                             "destination_account_no"])
    return loan


def _codes(report):
    return {f.code for f in report["findings"]}


def _find(report, code):
    return next(f for f in report["findings"] if f.code == code)


# ── A clean loan produces nothing ───────────────────────────────────────────
def test_a_properly_disbursed_loan_is_silent(coop, member, product):
    """Cash path: journal recorded, schedule built, no payout expected."""
    _acct(coop, "1000", "Cash", Account.Kind.ASSET)
    loan = _loan(coop, member, product, status=Loan.Status.APPROVED)
    with use_tenant(coop):
        disburse_loan(loan)

    report = loan_health(coop)

    assert report["findings"] == []
    assert report["defects"] == 0


def test_the_check_writes_nothing(coop, member, product):
    """It is a report, not a repair. Running it must not change anything."""
    _loan(coop, member, product, status=Loan.Status.DISBURSED,
          disbursed_at=timezone.now())
    journals_before = Journal.all_objects.count()

    loan_health(coop)
    loan_health(coop)

    assert Journal.all_objects.count() == journals_before


# ── The backlog is not a defect ─────────────────────────────────────────────
def test_an_approved_loan_is_a_queue_item_not_a_fault(coop, banked, product,
                                                      funded_wallet):
    loan = _loan(funded_wallet, banked, product, status=Loan.Status.APPROVED)

    report = loan_health(funded_wallet)

    finding = _find(report, AWAITING_PAYMENT)
    assert finding.severity == "info"
    assert finding.auto_fixable is False
    assert report["defects"] == 0, "a backlog must not count as damage"
    assert report["awaiting_payment"] == 1
    assert loan.pk == finding.loan_id


def test_it_says_when_an_approved_loan_is_ready_to_pay(coop, banked, product,
                                                       funded_wallet):
    _loan(funded_wallet, banked, product, status=Loan.Status.APPROVED)

    report = loan_health(funded_wallet)

    assert "Ready to pay" in _find(report, AWAITING_PAYMENT).detail


def test_it_says_when_there_is_no_bank_code(coop, member, product,
                                           funded_wallet):
    """Every member whose details predate bank selection is in this state."""
    _loan(funded_wallet, member, product, status=Loan.Status.APPROVED)

    report = loan_health(funded_wallet)

    detail = _find(report, AWAITING_PAYMENT).detail
    assert "No bank code" in detail


def test_it_says_when_the_wallet_is_short(coop, banked, product):
    _loan(coop, banked, product, status=Loan.Status.APPROVED)

    report = loan_health(coop)

    assert "wallet holds" in _find(report, AWAITING_PAYMENT).detail


# ── Real inconsistencies ────────────────────────────────────────────────────
def test_disbursed_without_a_journal_is_critical_and_manual(coop, member,
                                                            product):
    """A status claiming money moved with nothing in the books to support it."""
    _loan(coop, member, product, status=Loan.Status.DISBURSED,
          disbursed_at=timezone.now())

    report = loan_health(coop)

    finding = _find(report, DISBURSED_WITHOUT_JOURNAL)
    assert finding.severity == "critical"
    assert finding.auto_fixable is False
    assert "person" in finding.fix.lower()


def test_a_failed_payout_with_a_disbursed_loan_is_critical(coop, banked,
                                                           product,
                                                           funded_wallet):
    loan = _loan(funded_wallet, banked, product,
                 status=Loan.Status.DISBURSED, disbursed_at=timezone.now())
    with use_tenant(funded_wallet):
        Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=loan.pk,
            amount=loan.principal, status=Payout.Status.FAILED,
            reference="PO-x", failure_reason="Account name mismatch",
            destination_bank_code="044", destination_account_no="0123456789")

    report = loan_health(funded_wallet)

    finding = _find(report, PAYOUT_FAILED_STILL_DISBURSED)
    assert finding.severity == "critical"
    assert finding.auto_fixable is True
    assert "Account name mismatch" in finding.detail


def test_a_freshly_sent_payout_is_not_flagged(coop, banked, product,
                                              funded_wallet):
    """In flight is normal. Flagging it immediately would make every fresh
    disbursement noise and the report would stop being read."""
    loan = _loan(funded_wallet, banked, product,
                 status=Loan.Status.DISBURSED, disbursed_at=timezone.now())
    with use_tenant(funded_wallet):
        Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=loan.pk, amount=loan.principal,
            status=Payout.Status.PENDING, reference="PO-fresh",
            sent_at=timezone.now())

    report = loan_health(funded_wallet)

    assert PAYOUT_UNCONFIRMED not in _codes(report)


def test_a_long_unconfirmed_payout_is_flagged(coop, banked, product,
                                              funded_wallet):
    loan = _loan(funded_wallet, banked, product,
                 status=Loan.Status.DISBURSED, disbursed_at=timezone.now())
    with use_tenant(funded_wallet):
        Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=loan.pk, amount=loan.principal,
            status=Payout.Status.PENDING, reference="PO-stale",
            sent_at=timezone.now() - timedelta(hours=30))

    report = loan_health(funded_wallet)

    finding = _find(report, PAYOUT_UNCONFIRMED)
    assert finding.auto_fixable is True
    assert "30h" in finding.detail


def test_a_disbursed_loan_with_no_schedule_is_flagged(coop, member, product):
    """Nothing would ever fall due, so the loan can never go into arrears."""
    _acct(coop, "1000", "Cash", Account.Kind.ASSET)
    loan = _loan(coop, member, product, status=Loan.Status.APPROVED)
    with use_tenant(coop):
        disburse_loan(loan)
    RepaymentInstalment.all_objects.filter(loan=loan).delete()

    report = loan_health(coop)

    assert MISSING_SCHEDULE in _codes(report)


def test_an_approved_loan_with_a_settled_payout_is_critical(coop, banked,
                                                            product,
                                                            funded_wallet):
    """Either a reversal ran that should not have, or money went out twice."""
    loan = _loan(funded_wallet, banked, product, status=Loan.Status.APPROVED)
    with use_tenant(funded_wallet):
        Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=loan.pk, amount=loan.principal,
            status=Payout.Status.SUCCESS, reference="PO-paid")

    report = loan_health(funded_wallet)

    finding = _find(report, APPROVED_WITH_SUCCESSFUL_PAYOUT)
    assert finding.severity == "critical"
    assert finding.auto_fixable is False
    assert AWAITING_PAYMENT not in _codes(report), (
        "it must not also read as a routine queue item")


# ── Fixing ──────────────────────────────────────────────────────────────────
def test_fixing_a_missing_schedule_rebuilds_it(coop, member, product):
    _acct(coop, "1000", "Cash", Account.Kind.ASSET)
    loan = _loan(coop, member, product, status=Loan.Status.APPROVED)
    with use_tenant(coop):
        disburse_loan(loan)
    RepaymentInstalment.all_objects.filter(loan=loan).delete()

    with use_tenant(coop):
        result = fix_finding(_find(loan_health(coop), MISSING_SCHEDULE))

    assert "rebuilt" in result
    assert RepaymentInstalment.all_objects.filter(loan=loan).count() == 6


def test_fixing_a_failed_payout_reverts_the_loan(coop, banked, product,
                                                 funded_wallet):
    _acct(funded_wallet, "1000", "Cash", Account.Kind.ASSET)
    loan = _loan(funded_wallet, banked, product, status=Loan.Status.APPROVED)
    with use_tenant(funded_wallet):
        disburse_loan(loan)
        Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=loan.pk, amount=loan.principal,
            status=Payout.Status.FAILED, reference="PO-f",
            failure_reason="Bank rejected it")

        fix_finding(_find(loan_health(funded_wallet),
                          PAYOUT_FAILED_STILL_DISBURSED))

    loan.refresh_from_db()
    assert loan.status == Loan.Status.APPROVED
    assert loan.disbursed_at is None
    assert loan.disbursement_journal_id is None
    assert RepaymentInstalment.all_objects.filter(loan=loan).count() == 0


def test_fixing_is_audited(coop, member, product):
    from audit.models import AuditLog

    _acct(coop, "1000", "Cash", Account.Kind.ASSET)
    loan = _loan(coop, member, product, status=Loan.Status.APPROVED)
    with use_tenant(coop):
        disburse_loan(loan)
    RepaymentInstalment.all_objects.filter(loan=loan).delete()

    with use_tenant(coop):
        fix_finding(_find(loan_health(coop), MISSING_SCHEDULE))

    assert AuditLog.all_objects.filter(
        action=f"loan.health_fix.{MISSING_SCHEDULE}").exists()


def test_a_manual_finding_refuses_to_be_auto_fixed(coop, member, product):
    """The guard that keeps a batch job out of the ledger."""
    _loan(coop, member, product, status=Loan.Status.DISBURSED,
          disbursed_at=timezone.now())
    finding = _find(loan_health(coop), DISBURSED_WITHOUT_JOURNAL)

    with pytest.raises(ValueError) as exc:
        fix_finding(finding)

    assert "not safe to correct automatically" in str(exc.value)


# ── Scoping ─────────────────────────────────────────────────────────────────
def test_it_can_be_scoped_to_one_cooperative(coop, other_coop, member,
                                             product):
    _loan(coop, member, product, status=Loan.Status.DISBURSED,
          disbursed_at=timezone.now())

    assert loan_health(coop)["checked"] == 1
    assert loan_health(other_coop)["checked"] == 0
