"""
What an operator can actually *do* on the admin's read-only pages.

Several admins are immutable on purpose — a payout is the record of money already
handed to a provider, and the bank catalogue is pulled from that provider — so
editing them by hand would make our books disagree with what really happened.

But "not editable" must not mean "nothing can be done". Both of these pages
previously showed a record and offered no way to resolve it: a payout stuck on
*pending* (the case support is called about) and a stale or empty bank catalogue
(which blocks every electronic payout, and needed shell access to fix). The
legitimate operation belongs here as an **action**, routed through the same
service the officer-facing screens use so the two cannot disagree.

These tests also pin what must stay impossible: the ledger, the audit trail and
cast votes are append-only, and no amount of 2FA opens them.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from django.utils import timezone

from accounts.models import User
from core.context import use_tenant
from payments.admin import BankAdmin, PayoutAdmin
from payments.models import Bank, Payout

pytestmark = pytest.mark.django_db


def _request(user):
    request = RequestFactory().post("/admin/")
    request.user = user
    messages = []
    request._messages = type(
        "_S", (), {"add": lambda self, level, msg, extra="": messages.append(msg)})()
    request.collected = messages
    return request


@pytest.fixture
def operator():
    return User.objects.create_user(
        email="ops@startupripple.co", full_name="Ops", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=True)


@pytest.fixture
def no_2fa():
    return User.objects.create_user(
        email="plain@startupripple.co", full_name="Plain", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=False)


def _payout(coop, status=Payout.Status.PENDING, *, with_journal=False, **kw):
    """A payout row. ``with_journal`` attaches the entry that moved the money.

    Without one there is nothing for a failure to compensate — which is correct,
    but means a test of the reversal has to supply it.
    """
    from ledger.models import Account
    from ledger.services import Line, post_journal

    with use_tenant(coop):
        journal = None
        if with_journal:
            wallet, _ = Account.all_objects.get_or_create(
                cooperative=coop, code="1020",
                defaults={"name": "Disbursement Wallet",
                          "kind": Account.Kind.ASSET, "system": True})
            settle, _ = Account.all_objects.get_or_create(
                cooperative=coop, code="1010",
                defaults={"name": "Bank / PSP Settlement",
                          "kind": Account.Kind.ASSET, "system": True})
            journal = post_journal(
                cooperative=coop, reference="PO-xyz", memo="payout",
                lines=[Line(account=settle, debit=Decimal("1000.00")),
                       Line(account=wallet, credit=Decimal("1000.00"))])
        return Payout.objects.create(
            kind=Payout.Kind.LOAN, object_id=1, amount=Decimal("1000.00"),
            status=status, reference="PO-xyz", journal=journal, **kw)


# ── Payout: rechecking from the admin ───────────────────────────────────────
def test_an_operator_can_recheck_a_stuck_payout(coop, operator, monkeypatch):
    """The case support is called about: pending, and the money has gone."""
    from payments import services

    payout = _payout(coop, sent_at=timezone.now())

    class _Stub:
        def fetch_transfer(self, reference):
            return {"status": "success", "raw": {}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())
    request = _request(operator)

    PayoutAdmin(Payout, AdminSite()).recheck_selected(
        request, Payout.all_objects.filter(pk=payout.pk))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SUCCESS
    assert any("pending" in m and "success" in m for m in request.collected)


def test_rechecking_uses_the_same_path_as_the_webhook(coop, operator,
                                                      monkeypatch):
    """A failed transfer must unwind, not merely relabel — otherwise the admin
    and the console would hold different opinions about whether money moved."""
    from payments import services

    payout = _payout(coop, sent_at=timezone.now(), with_journal=True)

    class _Stub:
        def fetch_transfer(self, reference):
            return {"status": "failed", "raw": {"reason": "Rejected"}}

    monkeypatch.setattr(services, "get_provider", lambda n: _Stub())

    PayoutAdmin(Payout, AdminSite()).recheck_selected(
        _request(operator), Payout.all_objects.filter(pk=payout.pk))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.FAILED
    assert payout.reversal_journal_id is not None, (
        "the compensating reversal must have been posted")


def test_a_settled_payout_is_skipped_not_re_asked(coop, operator):
    payout = _payout(coop, status=Payout.Status.SUCCESS,
                     settled_at=timezone.now())
    request = _request(operator)

    PayoutAdmin(Payout, AdminSite()).recheck_selected(
        request, Payout.all_objects.filter(pk=payout.pk))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.SUCCESS
    assert any("already settled" in m for m in request.collected)


def test_a_provider_outage_is_reported_not_swallowed(coop, operator,
                                                     monkeypatch):
    from payments import services
    from payments.providers import PaymentInitError

    payout = _payout(coop, sent_at=timezone.now())

    class _Down:
        def fetch_transfer(self, reference):
            raise PaymentInitError("Could not reach Paystack.")

    monkeypatch.setattr(services, "get_provider", lambda n: _Down())
    request = _request(operator)

    PayoutAdmin(Payout, AdminSite()).recheck_selected(
        request, Payout.all_objects.filter(pk=payout.pk))

    payout.refresh_from_db()
    assert payout.status == Payout.Status.PENDING, "nothing may have changed"
    assert any("could not reach the provider" in m.lower()
               for m in request.collected)


def test_the_payout_page_stays_uneditable(operator):
    """The action exists *because* editing must not."""
    admin = PayoutAdmin(Payout, AdminSite())
    request = _request(operator)

    assert admin.has_change_permission(request) is False
    assert admin.has_add_permission(request) is False
    assert "recheck_selected" in admin.get_actions(request)


def test_rechecking_is_hidden_without_2fa(no_2fa):
    admin = PayoutAdmin(Payout, AdminSite())

    assert "recheck_selected" not in admin.get_actions(_request(no_2fa))


# ── Bank: refreshing the catalogue ──────────────────────────────────────────
def test_an_operator_can_refresh_the_bank_catalogue(operator, monkeypatch):
    """An empty catalogue blocks every electronic payout; fixing it should not
    need shell access."""
    from payments import providers

    class _Stub:
        def list_banks(self):
            return [{"name": "Access Bank", "code": "044", "slug": "access",
                     "currency": "NGN"},
                    {"name": "GTBank", "code": "058", "slug": "gtb",
                     "currency": "NGN"}]

    monkeypatch.setattr(providers, "get_provider", lambda n: _Stub())
    request = _request(operator)

    BankAdmin(Bank, AdminSite()).refresh_catalogue(request, Bank.objects.none())

    assert Bank.objects.count() == 2
    assert any("2 added" in m for m in request.collected)


def test_refreshing_says_so_when_there_is_no_live_key(operator):
    """The dev short-circuit returns nothing; that must read as "nothing
    happened", not as success."""
    request = _request(operator)

    BankAdmin(Bank, AdminSite()).refresh_catalogue(request, Bank.objects.none())

    assert Bank.objects.count() == 0
    assert any("no live provider key" in m for m in request.collected)


def test_refreshing_is_hidden_without_2fa(no_2fa):
    admin = BankAdmin(Bank, AdminSite())

    assert "refresh_catalogue" not in admin.get_actions(_request(no_2fa))


def test_the_bank_page_stays_uneditable(operator):
    """A hand-typed code would not fail visibly — it would point a payout at the
    wrong bank."""
    admin = BankAdmin(Bank, AdminSite())

    assert admin.has_add_permission(_request(operator)) is False
    assert admin.has_change_permission(_request(operator)) is False


# ── What must stay impossible ───────────────────────────────────────────────
@pytest.mark.parametrize("app_label,model_name", [
    ("ledger", "Journal"),
    ("ledger", "LedgerEntry"),
    ("audit", "AuditLog"),
    ("governance", "Vote"),
])
def test_the_append_only_records_resist_even_a_2fa_superuser(
        app_label, model_name, operator):
    """These are the integrity guarantees the product rests on. Corrections are
    made by posting a reversal, never by editing — so no flag may open them."""
    from django.apps import apps
    from django.contrib import admin as dj_admin

    model = apps.get_model(app_label, model_name)
    model_admin = dj_admin.site._registry[model]
    request = _request(operator)

    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_delete_permission(request) is False
