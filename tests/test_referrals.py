"""
Refer and earn.

Pinned: a cooperative signing up through a referral link is credited to the
referrer on the terms of that day; a bad, suspended or self-referring code
never costs a society its signup; each paid invoice earns its share once,
only inside the earning window; payouts need bank details, respect the
minimum and the balance, and a rejected one returns to the balance; and the
platform's endpoints are the platform's alone.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from platform_admin.billing import settle_invoice
from platform_admin.models import Invoice, Plan, Subscription
from referrals import services
from referrals.models import (Commission, Referral, ReferralPayout,
                              ReferralProgram, Referrer)
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db

PASSWORD = "Harvest-Season-2026"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def starter_plan():
    return Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                               price_monthly="10000")


@pytest.fixture
def referrer():
    user = User.objects.create_user(email="ref@example.com",
                                    full_name="Tola Referrer", password="x")
    r = services.become_referrer(user)
    r.bank_name, r.account_no, r.account_name = ("GTBank", "0123456789",
                                                 "Tola Referrer")
    r.save()
    return r


def _signup(**overrides):
    payload = {"society_name": "Aba Traders Multipurpose",
               "applicant_name": "Chidi Okafor",
               "applicant_email": "chidi@example.com", "password": PASSWORD,
               **overrides}
    return APIClient().post("/api/v1/public/apply/", payload, format="json")


def _society():
    return Cooperative.objects.get(name="Aba Traders Multipurpose")


def _invoice(coop, amount="10000.00", number="INV-1"):
    sub = Subscription.objects.get(cooperative=coop)
    return Invoice.objects.create(cooperative=coop, subscription=sub,
                                  number=number, amount=Decimal(amount),
                                  status=Invoice.Status.SENT)


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


# ── Becoming a referrer ────────────────────────────────────────────────────
def test_signing_up_to_refer_makes_an_account_with_a_code():
    res = APIClient().post("/api/v1/public/refer/", {
        "full_name": "Ife Ade", "email": "ife@example.com",
        "password": PASSWORD}, format="json")

    assert res.status_code == 201, res.data
    user = User.objects.get(email="ife@example.com")
    assert user.check_password(PASSWORD)
    assert Referrer.objects.get(user=user).code == res.data["code"]
    me = _client(user).get("/api/v1/me/")
    assert me.data["is_referrer"] is True and me.data["memberships"] == []


def test_an_existing_account_must_give_its_own_password(referrer):
    res = APIClient().post("/api/v1/public/refer/", {
        "full_name": "x", "email": "ref@example.com", "password": PASSWORD},
        format="json")
    assert res.status_code == 400


def test_a_member_can_turn_referrals_on_from_their_account(member):
    client = _client(member.user)
    assert client.get("/api/v1/me/referrer/").status_code == 404
    res = client.post("/api/v1/me/referrer/")
    assert res.status_code == 200
    assert res.data["link"].endswith(f"/signup?ref={res.data['code']}")


def test_no_new_referrers_while_the_program_is_off(member):
    ReferralProgram.objects.update_or_create(pk=1, defaults={"active": False})
    assert _client(member.user).post("/api/v1/me/referrer/").status_code == 400


# ── Credit at signup ───────────────────────────────────────────────────────
def test_a_society_signing_up_with_the_link_is_credited(
        starter_plan, referrer, django_capture_on_commit_callbacks):
    ReferralProgram.objects.update_or_create(
        pk=1, defaults={"commission_percent": Decimal("15"),
                        "commission_months": 6})
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        res = _signup(referral_code=referrer.code.lower())

    assert res.status_code == 201, res.data
    referral = Referral.objects.get(cooperative=_society())
    assert referral.referrer == referrer
    assert (referral.commission_percent, referral.commission_months) == (
        Decimal("15"), 6), "the terms of the day are copied"
    assert any(m.to == ["ref@example.com"] for m in mail.outbox)


@pytest.mark.parametrize("code", ["NOPE123", ""])
def test_a_bad_code_never_blocks_signup(starter_plan, code):
    assert _signup(referral_code=code).status_code == 201
    assert not Referral.objects.exists()


def test_a_suspended_referrer_is_not_credited(starter_plan, referrer):
    referrer.status = Referrer.Status.SUSPENDED
    referrer.save()
    assert _signup(referral_code=referrer.code).status_code == 201
    assert not Referral.objects.exists()


def test_you_cannot_refer_your_own_cooperative(starter_plan, referrer):
    res = _signup(referral_code=referrer.code,
                  applicant_email="ref@example.com", password="x")
    assert res.status_code == 201, res.data
    assert not Referral.objects.exists()


# ── Earning ────────────────────────────────────────────────────────────────
@pytest.fixture
def referred(starter_plan, referrer):
    _signup(referral_code=referrer.code)
    return _society()


def test_a_paid_invoice_earns_its_share_once(referred, referrer):
    invoice = _invoice(referred)
    settle_invoice(invoice)
    settle_invoice(invoice)
    services.credit_for_invoice(invoice)

    commission = Commission.objects.get()
    assert commission.amount == Decimal("1000.00")
    assert services.totals(referrer)["available"] == Decimal("1000.00")
    assert Referral.objects.get().first_paid_on == timezone.localdate()


def test_nothing_is_earned_after_the_window(referred, referrer):
    referral = Referral.objects.get()
    referral.first_paid_on = timezone.localdate() - timedelta(days=400)
    referral.save()
    settle_invoice(_invoice(referred))
    assert not Commission.objects.exists()


def test_an_invoice_paid_before_the_referral_earns_nothing(coop, referrer):
    Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                        price_monthly="10000")
    from platform_admin.billing import ensure_subscription
    ensure_subscription(coop)
    old = _invoice(coop, number="INV-OLD")
    settle_invoice(old, paid_on=timezone.localdate() - timedelta(days=30))

    services.attach_by_platform(coop, referrer=referrer, actor=None)
    services.credit_for_invoice(old)
    assert not Commission.objects.exists()

    settle_invoice(_invoice(coop, number="INV-NEW"))
    assert Commission.objects.count() == 1


def test_a_cooperative_has_one_referrer(referred, referrer):
    other = services.become_referrer(User.objects.create_user(
        email="b@example.com", full_name="B"))
    with pytest.raises(services.ReferralError):
        services.attach_by_platform(referred, referrer=other, actor=None)


def test_a_voided_commission_leaves_the_balance(referred, referrer):
    settle_invoice(_invoice(referred))
    services.void_commission(Commission.objects.get(), reason="Refunded",
                             actor=None)
    assert services.totals(referrer)["available"] == 0


# ── Payouts ────────────────────────────────────────────────────────────────
@pytest.fixture
def earned(referred, referrer):
    settle_invoice(_invoice(referred, amount="80000.00"))   # earns 8,000
    return referrer


def test_payout_rules(earned):
    client = _client(earned.user)
    url = "/api/v1/me/referrer/payouts/"
    assert client.post(url, {"amount": "1000"}).status_code == 400, "minimum"
    assert client.post(url, {"amount": "9000"}).status_code == 400, "balance"

    res = client.post(url, {"amount": "6000"})
    assert res.status_code == 201, res.data
    assert res.data["account_no"] == "0123456789"
    assert services.totals(earned)["available"] == Decimal("2000.00")


def test_no_payout_without_bank_details(earned):
    earned.account_no = ""
    earned.save()
    res = _client(earned.user).post("/api/v1/me/referrer/payouts/",
                                    {"amount": "6000"})
    assert res.status_code == 400


def test_the_platform_pays_or_rejects(earned,
                                     django_capture_on_commit_callbacks):
    ReferralProgram.objects.filter(pk=1).update(minimum_payout=1000)
    payout = services.request_payout(earned, amount="6000")
    admin = User.objects.create_user(email="ops@p.test", full_name="Ops",
                                     password="x", is_platform_admin=True)
    ops = _client(admin)
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        res = ops.post(
            f"/api/v1/platform-referral-payouts/{payout.id}/mark-paid/",
            {"reference": "TRF-991"})
    assert res.status_code == 200 and res.data["status"] == "paid"
    assert [m.to for m in mail.outbox] == [["ref@example.com"]]
    assert ops.post(f"/api/v1/platform-referral-payouts/{payout.id}/reject/"
                    ).status_code == 400, "decided once"

    second = services.request_payout(earned, amount="2000")
    ops.post(f"/api/v1/platform-referral-payouts/{second.id}/reject/",
             {"note": "Wrong account"})
    t = services.totals(earned)
    assert (t["paid"], t["available"]) == (Decimal("6000"), Decimal("2000"))


def test_platform_endpoints_are_platform_only(earned):
    client = _client(earned.user)
    for url in ["/api/v1/platform-referrers/",
                "/api/v1/platform-referral-payouts/",
                "/api/v1/platform-commissions/",
                "/api/v1/platform-referral-program/"]:
        assert client.get(url).status_code == 403, url


def test_the_dashboard_shows_referrals_and_earnings(earned):
    res = _client(earned.user).get("/api/v1/me/referrer/")
    assert res.status_code == 200
    assert res.data["totals"]["earned"] == "8000.00"
    assert res.data["referrals"][0]["cooperative_name"] == (
        "Aba Traders Multipurpose")
    assert res.data["referrals"][0]["earned"] == "8000.00"
    assert ReferralPayout.objects.count() == 0


def test_months_are_added_in_one_step_not_clamped_on_the_way():
    from datetime import date

    from platform_admin.billing import _add_months
    assert _add_months(date(2026, 10, 29), 12) == date(2027, 10, 29)
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert _add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
