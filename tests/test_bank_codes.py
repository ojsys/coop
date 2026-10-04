"""
Bank *codes* must survive the whole dual-control path.

Paystack identifies a bank by code, not by name: a settlement subaccount and a
member's transfer recipient are both created from ``(bank_code, account_no)``.
So a code that gets dropped anywhere between "an officer proposes new bank
details" and "the cooperative row is updated" does not fail loudly — it leaves
settlement pointed at whatever bank the *old* code names, while the screen shows
the new bank's name.

Carrying one field through that path touches eight places (the proposed dict,
the unchanged-comparison, the BankDetailChange create, then apply's before
dict, assignment, update_fields, after dict, and describe()). These tests pin
each end of it, because missing any one of them is invisible until money moves.
"""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from payments.models import Bank
from tenants.models import BankDetailChange
from tenants.services import (
    BankDetailError, apply_bank_detail_change, propose_bank_detail_change,
)

pytestmark = pytest.mark.django_db

NEW = {
    "bank_name": "Access Bank",
    "bank_code": "044",
    "bank_account_name": "Imole Cooperative Society",
    "bank_account_no": "0123456789",
}


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


# ── The code survives propose → apply ───────────────────────────────────────
def test_the_bank_code_is_carried_onto_the_cooperative(coop):
    """The whole point: apply must write the code, not just the name."""
    officer = _officer(coop, "maker@imole.coop")

    change, _ = propose_bank_detail_change(coop, actor=officer, **NEW)
    apply_bank_detail_change(change, actor=_officer(coop, "check@imole.coop"))

    coop.refresh_from_db()
    assert coop.bank_code == "044"
    assert coop.bank_name == "Access Bank"
    assert coop.bank_account_no == "0123456789"


def test_the_previous_code_is_captured_so_the_trail_survives(coop):
    with use_tenant(coop):
        coop.bank_name = "GTBank"
        coop.bank_code = "058"
        coop.bank_account_no = "9999999999"
        coop.save(update_fields=["bank_name", "bank_code", "bank_account_no"])
    officer = _officer(coop, "maker@imole.coop")

    change, _ = propose_bank_detail_change(coop, actor=officer, **NEW)

    assert change.previous_bank_code == "058", (
        "without this, an approved change overwrites the code with no record "
        "of what it replaced")
    assert change.previous_bank_name == "GTBank"


def test_the_approver_sees_the_code_beside_the_bank_name(coop):
    """describe() is the only text the Approvals page renders. A name without
    its code hides a mismatch between the two."""
    officer = _officer(coop, "maker@imole.coop")

    change, approval = propose_bank_detail_change(coop, actor=officer, **NEW)

    assert "Access Bank (044)" in change.describe()
    assert "Access Bank (044)" in approval.summary


def test_a_missing_code_still_reads_cleanly(coop):
    """Existing societies have a bank name and no code. The summary must not
    render "Access Bank ()" or similar."""
    officer = _officer(coop, "maker@imole.coop")

    change, _ = propose_bank_detail_change(
        coop, actor=officer, bank_name="Access Bank",
        bank_account_name="Imole", bank_account_no="0123456789")

    summary = change.describe()
    assert "Access Bank" in summary
    assert "()" not in summary


# ── A code alone is not a proposal ──────────────────────────────────────────
def test_a_bank_code_on_its_own_is_refused(coop):
    """It identifies a bank but names no account, so an officer could not
    meaningfully judge the request."""
    officer = _officer(coop, "maker@imole.coop")

    with pytest.raises(BankDetailError):
        propose_bank_detail_change(coop, actor=officer, bank_code="044")

    assert BankDetailChange.all_objects.count() == 0


# ── The profile PATCH bypass stays closed ───────────────────────────────────
def test_the_profile_patch_cannot_write_the_bank_code(coop):
    """The three bank fields were deliberately removed from
    CooperativeUpdateSerializer so a single officer cannot redirect payments.
    The code must not reopen that door."""
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).patch(
        f"/api/v1/cooperatives/{coop.id}/",
        {"bank_code": "044", "name": coop.name},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200, resp.content
    coop.refresh_from_db()
    assert coop.bank_code != "044", (
        "bank_code must go through propose/approve like the other three")


# ── The API carries it end to end ───────────────────────────────────────────
def test_the_propose_endpoint_accepts_a_bank_code(coop):
    """The view reads each field off request.data individually, so a field it
    does not name is silently discarded."""
    officer = _officer(coop, "maker@imole.coop")

    resp = _client(officer).post(
        f"/api/v1/cooperatives/{coop.id}/propose-bank-details/",
        NEW, format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 201, resp.content
    change = BankDetailChange.all_objects.get()
    assert change.bank_code == "044"
    assert "044" in resp.json()["summary"]


def test_a_member_can_record_their_own_bank_code(coop, member):
    """Needed to create their transfer recipient when a loan is disbursed."""
    resp = _client(member.user).patch(
        "/api/v1/me/profile/",
        {"bank_name": "Access Bank", "bank_code": "044",
         "bank_account_no": "0123456789"},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200, resp.content
    member.refresh_from_db()
    assert member.bank_code == "044"


# ── The catalogue ───────────────────────────────────────────────────────────
def test_the_reference_endpoint_serves_the_bank_catalogue(coop, member):
    Bank.objects.create(code="044", name="Access Bank", slug="access-bank")
    Bank.objects.create(code="058", name="GTBank", slug="gtbank")
    Bank.objects.create(code="999", name="Closed Bank", active=False)

    body = _client(member.user).get(
        "/api/v1/reference/", HTTP_X_COOPERATIVE_ID=str(coop.id)).json()

    codes = {b["code"] for b in body["banks"]}
    assert codes == {"044", "058"}, "inactive banks must not be offered"


def test_the_catalogue_is_empty_rather_than_failing_without_a_live_key(coop,
                                                                      member):
    """With the dev placeholder key the provider call is skipped entirely. An
    empty list means "no verified catalogue", and the frontend falls back to a
    text input — a form must never break because Paystack is unreachable."""
    assert Bank.refresh() == {"fetched": 0, "created": 0, "updated": 0,
                              "deactivated": 0}

    body = _client(member.user).get(
        "/api/v1/reference/", HTTP_X_COOPERATIVE_ID=str(coop.id)).json()
    assert body["banks"] == []


def test_refresh_keeps_a_dropped_bank_but_marks_it_inactive(coop, monkeypatch):
    """A member may still hold the code of a bank the provider has dropped."""
    Bank.objects.create(code="999", name="Old Bank")

    from payments import providers

    class _Stub:
        def list_banks(self):
            return [{"code": "044", "name": "Access Bank",
                     "slug": "access-bank", "currency": "NGN"}]

    monkeypatch.setattr(providers, "get_provider", lambda name: _Stub())

    summary = Bank.refresh()

    assert summary["created"] == 1
    assert summary["deactivated"] == 1
    assert Bank.objects.get(code="999").active is False, (
        "dropped banks are deactivated, not deleted")
