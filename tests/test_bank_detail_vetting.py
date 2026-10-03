"""
Changing where a cooperative's money settles takes two officers.

The collection account is the one field where a single mistaken or compromised
officer could redirect every future payment, so it does not take effect when
saved: it is proposed, and applied only when a *different* privileged officer
approves.

Two bypasses are pinned here as much as the happy path. The profile PATCH must
no longer accept the bank fields — otherwise the whole control is sidestepped by
editing the society profile. And approving must require a privileged officer:
the approvals endpoint was IsAuthenticated, so any member at all counted as the
checker and dual control was enforced against the submitter alone.
"""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from tenants.models import BankDetailChange

pytestmark = pytest.mark.django_db

NEW = {
    "bank_name": "Access Bank",
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


def _propose(client, coop, **overrides):
    return client.post(
        f"/api/v1/cooperatives/{coop.id}/propose-bank-details/",
        {**NEW, **overrides}, format="json",
        HTTP_X_COOPERATIVE_ID=str(coop.id),
    )


def _pending_approval_id(client, coop):
    resp = client.get("/api/v1/approvals/",
                      HTTP_X_COOPERATIVE_ID=str(coop.id))
    rows = resp.json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    return next(r["id"] for r in rows if r["status"] == "pending")


# ── Proposing ───────────────────────────────────────────────────────────────
def test_proposing_does_not_change_the_account(coop):
    maker = _officer(coop, "maker@imole.coop")

    resp = _propose(_client(maker), coop)

    assert resp.status_code == 201, resp.content
    coop.refresh_from_db()
    assert coop.bank_account_no == "", "the account must not move until approved"
    assert BankDetailChange.all_objects.count() == 1


def test_the_approver_can_see_what_is_changing(coop):
    """Approving a change you cannot see is worse than no control at all."""
    maker = _officer(coop, "maker@imole.coop")

    resp = _propose(_client(maker), coop)

    assert "0123456789" in resp.json()["summary"]
    assert "not set" in resp.json()["summary"], "it should show what it replaces"


def test_a_no_op_proposal_is_refused(coop):
    coop.bank_name = NEW["bank_name"]
    coop.bank_account_name = NEW["bank_account_name"]
    coop.bank_account_no = NEW["bank_account_no"]
    coop.save()
    maker = _officer(coop, "maker@imole.coop")

    resp = _propose(_client(maker), coop)

    assert resp.status_code == 400
    assert "already" in resp.json()["detail"]


def test_an_empty_proposal_is_refused(coop):
    maker = _officer(coop, "maker@imole.coop")

    resp = _propose(_client(maker), coop, bank_name="",
                    bank_account_name="", bank_account_no="")

    assert resp.status_code == 400


# ── The bypass that would make this decorative ──────────────────────────────
def test_the_profile_patch_can_no_longer_write_bank_details(coop):
    """Otherwise the whole control is sidestepped by editing the profile."""
    officer = _officer(coop, "sec@imole.coop")

    resp = _client(officer).patch(
        f"/api/v1/cooperatives/{coop.id}/",
        {"bank_account_no": "9999999999", "name": coop.name},
        format="json", HTTP_X_COOPERATIVE_ID=str(coop.id),
    )

    assert resp.status_code == 200, resp.content
    coop.refresh_from_db()
    assert coop.bank_account_no != "9999999999"


# ── Approving ───────────────────────────────────────────────────────────────
def test_the_maker_cannot_approve_their_own_change(coop):
    maker = _officer(coop, "maker@imole.coop")
    client = _client(maker)
    _propose(client, coop)
    approval_id = _pending_approval_id(client, coop)

    resp = client.post(f"/api/v1/approvals/{approval_id}/approve/",
                       HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 400
    coop.refresh_from_db()
    assert coop.bank_account_no == ""


def test_a_second_officer_applies_the_change(coop):
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    _propose(_client(maker), coop)
    approval_id = _pending_approval_id(_client(maker), coop)

    resp = _client(checker).post(f"/api/v1/approvals/{approval_id}/approve/",
                                 HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 200, resp.content
    coop.refresh_from_db()
    assert coop.bank_account_no == "0123456789"
    assert coop.bank_name == "Access Bank"
    assert BankDetailChange.all_objects.get().is_applied


def test_rejecting_leaves_the_account_alone(coop):
    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    _propose(_client(maker), coop)
    approval_id = _pending_approval_id(_client(maker), coop)

    _client(checker).post(f"/api/v1/approvals/{approval_id}/reject/",
                          HTTP_X_COOPERATIVE_ID=str(coop.id))

    coop.refresh_from_db()
    assert coop.bank_account_no == ""
    assert not BankDetailChange.all_objects.get().is_applied


def test_an_ordinary_member_cannot_approve(coop):
    """The hole this closes: the endpoint was IsAuthenticated, so any member
    counted as the checker."""
    maker = _officer(coop, "maker@imole.coop")
    _propose(_client(maker), coop)
    approval_id = _pending_approval_id(_client(maker), coop)
    rank = _officer(coop, "rank@imole.coop", slug=Role.MEMBER)

    resp = _client(rank).post(f"/api/v1/approvals/{approval_id}/approve/",
                              HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert resp.status_code == 403
    coop.refresh_from_db()
    assert coop.bank_account_no == ""


def test_the_change_is_audited(coop):
    """This is where money starts going somewhere new."""
    from audit.models import AuditLog

    maker = _officer(coop, "maker@imole.coop")
    checker = _officer(coop, "checker@imole.coop")
    _propose(_client(maker), coop)
    approval_id = _pending_approval_id(_client(maker), coop)
    _client(checker).post(f"/api/v1/approvals/{approval_id}/approve/",
                          HTTP_X_COOPERATIVE_ID=str(coop.id))

    entry = AuditLog.all_objects.filter(
        action="cooperative.bank_details_changed").first()
    assert entry is not None
    assert entry.before["bank_account_no"] == ""
    assert entry.after["bank_account_no"] == "0123456789"
