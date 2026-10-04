"""
Connecting a cooperative's settlement subaccount.

A subaccount is how collections reach the society's *own* bank instead of the
platform's account. Paystack identifies the settlement bank by **code**, so a
society whose record holds only a bank name cannot have one created — which is
every society predating bank selection.

The permission test here matters more than the rest. ``ProviderAccountViewSet``
was ``IsAuthenticated`` on a full ModelViewSet, and a subaccount code is *where
money lands*: any member could POST their own code and redirect every future
contribution to their own bank account, PATCH an existing row, or DELETE it —
and since ``initialize_payment`` filters on ``connected=True``, flipping that
single field silently reverted settlement to the platform.
"""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from audit.models import AuditLog
from core.context import use_tenant
from payments.models import Provider, ProviderAccount
from payments.services import SubaccountError, ensure_subaccount

pytestmark = pytest.mark.django_db

CODE = "ACCT_imole_test01"


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


@pytest.fixture
def banked(coop):
    """A society with complete bank details, including the code."""
    coop.bank_name = "Access Bank"
    coop.bank_code = "044"
    coop.bank_account_name = "Imole Cooperative Society"
    coop.bank_account_no = "0123456789"
    coop.save(update_fields=["bank_name", "bank_code", "bank_account_name",
                             "bank_account_no"])
    return coop


@pytest.fixture
def stub_provider(monkeypatch):
    """Stand in for Paystack and record what it was asked to create."""
    calls = []

    class _Stub:
        def create_subaccount(self, **kwargs):
            calls.append(kwargs)
            return CODE

    from payments import services

    monkeypatch.setattr(services, "get_provider", lambda name: _Stub())
    return calls


# ── Incomplete bank details ─────────────────────────────────────────────────
def test_a_society_without_a_bank_code_cannot_be_connected(coop):
    """Every society predating bank selection is in this state."""
    coop.bank_name = "Access Bank"
    coop.bank_account_no = "0123456789"
    coop.save(update_fields=["bank_name", "bank_account_no"])

    with pytest.raises(SubaccountError) as exc:
        ensure_subaccount(coop)

    assert "bank code" in str(exc.value)
    assert ProviderAccount.all_objects.count() == 0


def test_the_error_names_everything_that_is_missing(coop):
    with pytest.raises(SubaccountError) as exc:
        ensure_subaccount(coop)

    message = str(exc.value)
    for expected in ("bank name", "bank code", "account number"):
        assert expected in message


# ── Creating one ────────────────────────────────────────────────────────────
def test_it_creates_and_stores_the_subaccount(banked, stub_provider):
    account, created = ensure_subaccount(banked)

    assert created is True
    assert account.subaccount_code == CODE
    assert account.provider == Provider.PAYSTACK
    assert account.connected is True
    assert account.cooperative_id == banked.id


def test_it_sends_the_bank_code_and_configured_percentage(banked,
                                                          stub_provider,
                                                          settings):
    settings.PAYSTACK_SUBACCOUNT_PERCENTAGE = 0

    ensure_subaccount(banked)

    sent = stub_provider[0]
    assert sent["bank_code"] == "044"
    assert sent["account_number"] == "0123456789"
    assert sent["percentage_charge"] == 0
    assert sent["business_name"] == "Imole Cooperative Society"


def test_it_falls_back_to_the_society_name_as_business_name(banked,
                                                           stub_provider):
    banked.bank_account_name = ""
    banked.save(update_fields=["bank_account_name"])

    ensure_subaccount(banked)

    assert stub_provider[0]["business_name"] == banked.name


def test_connecting_twice_does_not_create_a_second_subaccount(banked,
                                                              stub_provider):
    """A double-clicked button must not split a society's settlement in two."""
    first, created_first = ensure_subaccount(banked)
    second, created_second = ensure_subaccount(banked)

    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk
    assert len(stub_provider) == 1, "the provider must not be called again"
    assert ProviderAccount.all_objects.count() == 1


def test_it_is_audited(banked, stub_provider):
    officer = _officer(banked)

    ensure_subaccount(banked, actor=officer)

    entry = AuditLog.all_objects.filter(
        action="cooperative.subaccount_connected").first()
    assert entry is not None
    assert entry.actor_id == officer.pk
    assert entry.after["subaccount_code"] == CODE


def test_without_a_live_key_it_refuses_rather_than_storing_a_fake(banked,
                                                                 settings):
    """The dev placeholder key makes no network call and returns None. Storing a
    row anyway would tell the society its money settles to its own bank."""
    settings.PAYSTACK_SECRET_KEY = "sk_test_dev"

    with pytest.raises(SubaccountError) as exc:
        ensure_subaccount(banked)

    assert "no live payment-provider key" in str(exc.value).lower()
    assert ProviderAccount.all_objects.count() == 0


# ── The endpoint ────────────────────────────────────────────────────────────
def test_an_officer_can_connect_it_over_the_api(banked, stub_provider):
    officer = _officer(banked)

    resp = _client(officer).post(
        f"/api/v1/cooperatives/{banked.id}/connect-settlement-account/",
        HTTP_X_COOPERATIVE_ID=str(banked.id))

    assert resp.status_code == 201, resp.content
    assert resp.json()["subaccount_code"] == CODE
    assert resp.json()["created"] is True


def test_calling_the_endpoint_twice_answers_200_and_does_not_duplicate(
        banked, stub_provider):
    officer = _officer(banked)
    client = _client(officer)
    url = f"/api/v1/cooperatives/{banked.id}/connect-settlement-account/"

    client.post(url, HTTP_X_COOPERATIVE_ID=str(banked.id))
    again = client.post(url, HTTP_X_COOPERATIVE_ID=str(banked.id))

    assert again.status_code == 200
    assert again.json()["created"] is False
    assert ProviderAccount.all_objects.count() == 1


def test_incomplete_details_answer_400_with_the_reason(coop, stub_provider):
    officer = _officer(coop)

    resp = _client(officer).post(
        f"/api/v1/cooperatives/{coop.id}/connect-settlement-account/",
        HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 400
    assert "bank code" in resp.json()["detail"]


# ── The settlement-redirect hole ────────────────────────────────────────────
def test_a_member_cannot_create_a_provider_account(coop, member):
    """The worst of the open endpoints: a subaccount code is where money lands."""
    resp = _client(member.user).post(
        "/api/v1/provider-accounts/",
        {"provider": "paystack", "subaccount_code": "ACCT_attacker",
         "bank_name": "Attacker Bank", "connected": True},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 403
    assert ProviderAccount.all_objects.count() == 0


def test_a_member_cannot_redirect_an_existing_subaccount(coop, member):
    with use_tenant(coop):
        account = ProviderAccount.objects.create(
            provider=Provider.PAYSTACK, subaccount_code=CODE,
            bank_name="Access Bank")

    resp = _client(member.user).patch(
        f"/api/v1/provider-accounts/{account.id}/",
        {"subaccount_code": "ACCT_attacker"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 403
    account.refresh_from_db()
    assert account.subaccount_code == CODE


def test_a_member_cannot_disconnect_settlement(coop, member):
    """connected=False silently reverts the society to platform settlement."""
    with use_tenant(coop):
        account = ProviderAccount.objects.create(
            provider=Provider.PAYSTACK, subaccount_code=CODE,
            bank_name="Access Bank")

    resp = _client(member.user).patch(
        f"/api/v1/provider-accounts/{account.id}/", {"connected": False},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 403
    account.refresh_from_db()
    assert account.connected is True


def test_a_member_cannot_delete_a_provider_account(coop, member):
    with use_tenant(coop):
        account = ProviderAccount.objects.create(
            provider=Provider.PAYSTACK, subaccount_code=CODE)

    resp = _client(member.user).delete(
        f"/api/v1/provider-accounts/{account.id}/",
        HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 403
    assert ProviderAccount.all_objects.filter(pk=account.pk).exists()


def test_reads_stay_open_to_any_office_holder(coop, member):
    """Deliberately not closed.

    The console is reachable by any member holding a role — a Chairperson sits
    on it for approvals — and SettingsPage's own principle is "writable only by
    a privileged member, but do not render a card that will only ever 403".
    Closing reads would break that page for an office-holder entitled to see,
    but not change, where the society's money settles. Every write above is
    still refused.
    """
    resp = _client(member.user).get("/api/v1/provider-accounts/",
                                    HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200


def test_an_officer_can_still_read_them(coop):
    officer = _officer(coop)

    resp = _client(officer).get("/api/v1/provider-accounts/",
                                HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200
