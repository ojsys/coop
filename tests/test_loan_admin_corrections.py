"""
Correcting a loan's status from the admin.

The admin is the most privileged surface in the product, and the loan status is
the one field on it that money depends on. Crossing into or out of **disbursed**
posts a journal, builds a repayment schedule and stamps a date; typing the status
by hand does none of that, so the loan book would contradict the accounts.

These tests pin both halves: the hand edit is refused, and the actions that
replace it unwind the disbursement properly — by posting a reversal, never by
deleting the original entry.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from accounts.models import Membership, Role, User
from core.context import use_tenant
from ledger.models import Account, Journal
from ledger.services import Line, post_journal
from loans.admin import LoanAdmin, LoanAdminForm
from loans.models import Loan, LoanProduct, RepaymentInstalment
from loans.services import (LoanError, disburse_loan, return_to_pending,
                            unwind_disbursement)

pytestmark = pytest.mark.django_db


@pytest.fixture
def product(coop):
    with use_tenant(coop):
        return LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)


@pytest.fixture
def operator(coop):
    """A platform operator with 2FA on — what the admin requires."""
    return User.objects.create_user(
        email="ops@startupripple.co", full_name="Ops", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=True)


@pytest.fixture
def disbursed(coop, member, product):
    """A loan paid in cash — journal posted, schedule built."""
    with use_tenant(coop):
        Account.all_objects.get_or_create(
            cooperative=coop, code="1000",
            defaults={"name": "Cash", "kind": Account.Kind.ASSET,
                      "system": True})
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("100000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)
        # disburse_loan returns the journal it posted, not the loan.
        disburse_loan(loan)
    loan.refresh_from_db()
    return loan


def _admin():
    return LoanAdmin(Loan, AdminSite())


def _request(user):
    request = RequestFactory().post("/admin/loans/loan/")
    request.user = user
    # The message framework is not installed on a bare RequestFactory request.
    request._messages = type("_S", (), {"add": lambda self, *a, **k: None})()
    return request


# ── The guard on the field ──────────────────────────────────────────────────
def test_the_form_refuses_moving_a_disbursed_loan_by_hand(disbursed):
    form = LoanAdminForm(
        instance=disbursed,
        data={"cooperative": disbursed.cooperative_id,
              "membership": disbursed.membership_id,
              "product": disbursed.product_id,
              "status": Loan.Status.PENDING,
              "principal": "100000", "interest_rate": "10",
              "term_months": "6", "purpose": ""})

    assert not form.is_valid()
    assert "Reverse disbursement" in str(form.errors["status"])


def test_the_form_refuses_marking_a_loan_disbursed_by_hand(coop, member,
                                                           product):
    """It would move no money and build no schedule, so nothing would fall due."""
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)

    form = LoanAdminForm(
        instance=loan,
        data={"cooperative": loan.cooperative_id,
              "membership": loan.membership_id, "product": loan.product_id,
              "status": Loan.Status.DISBURSED,
              "principal": "50000", "interest_rate": "10",
              "term_months": "6", "purpose": ""})

    assert not form.is_valid()
    assert "no repayment schedule" in str(form.errors["status"])


def test_a_harmless_status_change_is_still_allowed(coop, member, product):
    """The guard is about the disbursed boundary, not about locking the field."""
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.PENDING)

    form = LoanAdminForm(
        instance=loan,
        data={"cooperative": loan.cooperative_id,
              "membership": loan.membership_id, "product": loan.product_id,
              "status": Loan.Status.REJECTED,
              "principal": "50000", "interest_rate": "10",
              "term_months": "6", "purpose": ""})

    # Asserting on the status field rather than the whole form: a bare form
    # builds its FK choices from the tenant-scoped managers, which are empty with
    # no tenant bound. The real admin fills them via UnscopedAdminMixin.
    assert "status" not in form.errors


# ── Unwinding properly ──────────────────────────────────────────────────────
def test_unwinding_posts_a_reversal_and_keeps_the_original(disbursed):
    """Corrections are made by mirror image; the original entry is never touched."""
    original = disbursed.disbursement_journal
    assert original is not None

    with use_tenant(disbursed.cooperative):
        unwind_disbursement(disbursed, actor=None)

    original.refresh_from_db()
    assert Journal.all_objects.filter(pk=original.pk).exists()
    assert original.is_reversed
    assert Journal.all_objects.filter(reversal_of=original).exists()


def test_unwinding_returns_the_loan_to_approved_and_clears_the_schedule(
        disbursed):
    with use_tenant(disbursed.cooperative):
        unwind_disbursement(disbursed, actor=None)

    disbursed.refresh_from_db()
    assert disbursed.status == Loan.Status.APPROVED
    assert disbursed.disbursed_at is None
    assert disbursed.disbursement_journal_id is None
    assert RepaymentInstalment.all_objects.filter(loan=disbursed).count() == 0


def test_unwinding_leaves_the_cash_account_square(disbursed):
    """The point of the reversal: the books stop showing money that came back."""
    from ledger.services import account_balance

    coop = disbursed.cooperative
    with use_tenant(coop):
        cash = Account.all_objects.get(cooperative=coop, code="1000")
        before = account_balance(cash)
        unwind_disbursement(disbursed, actor=None)
        after = account_balance(cash)

    assert after == before + disbursed.principal


def test_unwinding_a_loan_that_was_never_disbursed_is_refused(coop, member,
                                                              product):
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)

        with pytest.raises(LoanError) as exc:
            unwind_disbursement(loan)

    assert "no disbursement to undo" in str(exc.value)


# ── Returning to pending ────────────────────────────────────────────────────
def test_returning_to_pending_clears_the_approval(coop, member, product,
                                                  operator):
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED, decided_by=operator)

        return_to_pending(loan, actor=operator)

    loan.refresh_from_db()
    assert loan.status == Loan.Status.PENDING
    assert loan.decided_by_id is None
    assert loan.decided_at is None


def test_returning_a_disbursed_loan_to_pending_is_refused(disbursed):
    """It would reverse a ledger entry as a side effect of a status change."""
    with use_tenant(disbursed.cooperative):
        with pytest.raises(LoanError) as exc:
            return_to_pending(disbursed)

    assert "Reverse the disbursement first" in str(exc.value)
    disbursed.refresh_from_db()
    assert disbursed.status == Loan.Status.DISBURSED


def test_returning_to_pending_is_audited(coop, member, product, operator):
    from audit.models import AuditLog

    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.APPROVED)
        return_to_pending(loan, actor=operator)

    entry = AuditLog.all_objects.get(action="loan.return_to_pending")
    assert entry.actor_id == operator.id
    assert entry.before["status"] == "approved"


# ── The admin actions ───────────────────────────────────────────────────────
def test_the_reverse_action_unwinds_the_selected_loans(disbursed, operator):
    _admin().reverse_disbursement(
        _request(operator), Loan.all_objects.filter(pk=disbursed.pk))

    disbursed.refresh_from_db()
    assert disbursed.status == Loan.Status.APPROVED
    assert disbursed.disbursement_journal_id is None


def test_the_reverse_action_records_who_did_it(disbursed, operator):
    _admin().reverse_disbursement(
        _request(operator), Loan.all_objects.filter(pk=disbursed.pk))

    reversal = Journal.all_objects.filter(reversal_of__isnull=False).first()
    assert reversal is not None
    assert reversal.created_by_id == operator.id


def test_the_return_action_refuses_a_disbursed_loan(disbursed, operator):
    """The two actions are ordered on purpose and the second will not shortcut."""
    _admin().return_to_pending(
        _request(operator), Loan.all_objects.filter(pk=disbursed.pk))

    disbursed.refresh_from_db()
    assert disbursed.status == Loan.Status.DISBURSED


def test_the_two_actions_together_take_disbursed_to_pending(disbursed,
                                                            operator):
    """The operator's actual goal, reached without breaking the ledger."""
    admin, request = _admin(), _request(operator)
    qs = Loan.all_objects.filter(pk=disbursed.pk)

    admin.reverse_disbursement(request, qs)
    admin.return_to_pending(request, qs)

    disbursed.refresh_from_db()
    assert disbursed.status == Loan.Status.PENDING
    assert disbursed.disbursement_journal_id is None
    assert RepaymentInstalment.all_objects.filter(loan=disbursed).count() == 0


# ── 2FA, the reason the admin looked read-only in the first place ────────────
def test_both_actions_are_hidden_without_two_factor(coop):
    plain = User.objects.create_user(
        email="no2fa@startupripple.co", full_name="No 2FA", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=False)

    actions = _admin().get_actions(_request(plain))

    assert "reverse_disbursement" not in actions
    assert "return_to_pending" not in actions


def test_the_loan_admin_is_read_only_without_two_factor(coop):
    """Superusers are deliberately not exempt — this is why the section looked
    uneditable."""
    plain = User.objects.create_user(
        email="no2fa2@startupripple.co", full_name="No 2FA", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=False)
    admin, request = _admin(), _request(plain)

    assert admin.has_change_permission(request) is False
    assert admin.has_add_permission(request) is False


def test_with_two_factor_the_loan_admin_is_editable(operator):
    admin, request = _admin(), _request(operator)

    assert admin.has_change_permission(request) is True
    assert "reverse_disbursement" in admin.get_actions(request)


# ── End to end through the real admin view ──────────────────────────────────
#
# The permission-method tests above assert what LoanAdmin *reports*. These drive
# the actual change view, because "the loan record is editable" is a claim about
# the rendered page and a save that persists — a second blocker (a readonly_fields
# entry, a form error, a failing guard) would pass the method checks and still
# leave the operator looking at a page they cannot change.
def test_the_change_page_renders_with_a_save_button_when_2fa_is_on(
        client, disbursed, operator):
    client.force_login(operator)

    resp = client.get(f"/admin/loans/loan/{disbursed.pk}/change/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Change loan" in body
    assert 'name="_save"' in body, "no Save button — the form is read-only"


def test_the_change_page_is_read_only_without_2fa(client, disbursed):
    plain = User.objects.create_user(
        email="view@startupripple.co", full_name="Viewer", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=False)
    client.force_login(plain)

    resp = client.get(f"/admin/loans/loan/{disbursed.pk}/change/")

    body = resp.content.decode()
    assert resp.status_code == 200, "viewing stays allowed"
    assert 'name="_save"' not in body
    assert "View loan" in body, (
        "Django titles the page 'View loan' when change permission is denied — "
        "this is exactly what an operator without 2FA sees")


def _form_data(loan, **overrides):
    """What the admin form posts back, inlines included.

    The management-form keys are required: Django rejects a POST without them,
    and omitting one would fail the save for a reason unrelated to permissions.
    """
    data = {
        "cooperative": loan.cooperative_id,
        "membership": loan.membership_id,
        "product": loan.product_id,
        "status": loan.status,
        "principal": str(loan.principal),
        "interest_rate": str(loan.interest_rate),
        "term_months": str(loan.term_months),
        "purpose": loan.purpose,
        "destination_bank_name": loan.destination_bank_name,
        "destination_bank_code": loan.destination_bank_code,
        "destination_account_no": loan.destination_account_no,
        "instalments-TOTAL_FORMS": "0",
        "instalments-INITIAL_FORMS": "0",
        "instalments-MIN_NUM_FORMS": "0",
        "instalments-MAX_NUM_FORMS": "1000",
        "repayments-TOTAL_FORMS": "0",
        "repayments-INITIAL_FORMS": "0",
        "repayments-MIN_NUM_FORMS": "0",
        "repayments-MAX_NUM_FORMS": "1000",
        "_save": "Save",
    }
    if loan.disbursement_journal_id:
        data["disbursement_journal"] = loan.disbursement_journal_id
    data.update(overrides)
    return data


def test_an_edit_actually_saves(client, disbursed, operator):
    """The claim that matters: a change persists."""
    client.force_login(operator)

    resp = client.post(f"/admin/loans/loan/{disbursed.pk}/change/",
                      _form_data(disbursed, purpose="School fees — corrected"))

    assert resp.status_code == 302, getattr(resp, "context", None) and \
        resp.context["adminform"].form.errors
    disbursed.refresh_from_db()
    assert disbursed.purpose == "School fees — corrected"


def test_a_status_change_that_moves_no_money_saves(client, coop, member,
                                                   product, operator):
    client.force_login(operator)
    with use_tenant(coop):
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6,
            status=Loan.Status.PENDING)

    resp = client.post(f"/admin/loans/loan/{loan.pk}/change/",
                      _form_data(loan, status=Loan.Status.REJECTED))

    assert resp.status_code == 302
    loan.refresh_from_db()
    assert loan.status == Loan.Status.REJECTED


def test_the_dangerous_status_edit_is_refused_in_the_real_view(client,
                                                               disbursed,
                                                               operator):
    """Re-renders the form with the guard's message instead of saving."""
    client.force_login(operator)

    resp = client.post(f"/admin/loans/loan/{disbursed.pk}/change/",
                      _form_data(disbursed, status=Loan.Status.PENDING))

    assert resp.status_code == 200, "a refused save re-renders, it does not redirect"
    assert "Reverse disbursement" in resp.content.decode()
    disbursed.refresh_from_db()
    assert disbursed.status == Loan.Status.DISBURSED


def test_both_actions_are_offered_on_the_changelist(client, disbursed,
                                                    operator):
    client.force_login(operator)

    body = client.get("/admin/loans/loan/").content.decode()

    assert "Reverse disbursement (posts a ledger reversal)" in body
    assert "Return to pending (un-approve)" in body
