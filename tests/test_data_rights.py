"""
A person's own data: a copy of all of it, and deleting their account.

Deletion is pinned against the privacy notice's two lists — what is deleted
(credentials, contact, KYC, bank, photo, documents) and what is kept (ledger,
contribution and loan history, audit) — and against the rule that nobody is
erased while money is still owed either way.
"""
from __future__ import annotations

import io
import json
import zipfile
from decimal import Decimal

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from accounts.data_rights import deletion_token
from accounts.models import (AccountDeletionRequest, MemberDocument,
                             Membership, Role, User)
from contributions.models import Contribution
from contributions.services import record_contribution
from core.context import use_tenant
from ledger.models import LedgerEntry

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


@pytest.fixture
def secretary(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@imole.coop",
                                        full_name="Sec Retary", password="x")
        Membership.objects.create(
            user=user, member_no="SEC-1",
            role=Role.objects.filter(slug=Role.SECRETARY).first())
    return user


@pytest.fixture
def settled(member):
    """A member with nothing owed either way: share capital returned."""
    member.share_capital = Decimal("0")
    member.address = "1 Market Road"
    member.bank_account_no = "0123456789"
    member.save()
    with use_tenant(member.cooperative):
        MemberDocument.objects.create(
            membership=member, doc_type=MemberDocument.DocType.ID,
            file=SimpleUploadedFile("id.pdf", b"my id"))
    return member


def test_export_contains_profile_statement_and_files(coop, member, dues_type):
    record_contribution(cooperative=coop, membership=member,
                        contribution_type=dues_type, amount="5000",
                        channel="cash")
    with use_tenant(coop):
        MemberDocument.objects.create(
            membership=member, doc_type=MemberDocument.DocType.ID,
            label="NIN slip", file=SimpleUploadedFile("nin.pdf", b"NIN"))

    resp = _client(member.user, coop).get("/api/v1/me/data-export/")

    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert "account.json" in names
    assert json.loads(zf.read("account.json"))["email"] == member.user.email
    assert json.loads(zf.read("imole/profile.json"))["member_no"] == "IMC-0847"
    assert "5000" in zf.read("imole/statement.csv").decode()
    assert len(json.loads(zf.read("imole/contributions.json"))) == 1
    doc = next(n for n in names if n.startswith("imole/documents/"))
    assert zf.read(doc) == b"NIN"


def test_blockers_list_what_must_be_settled(coop, member):
    """The fixture member still has share capital to be returned."""
    data = _client(member.user, coop).get(
        "/api/v1/me/deletion-request/").json()

    assert data["request"] is None
    assert any("share capital" in b["message"] for b in data["blockers"])


def test_request_is_idempotent_and_cancellable(
        coop, member, secretary, django_capture_on_commit_callbacks):
    client = _client(member.user, coop)

    with django_capture_on_commit_callbacks(execute=True):
        first = client.post("/api/v1/me/deletion-request/",
                            {"reason": "Moving abroad"}, format="json")
    second = client.post("/api/v1/me/deletion-request/", {}, format="json")

    assert first.status_code == 201
    assert first.json()["request"]["id"] == second.json()["request"]["id"]
    # The officers and the member are both told.
    subjects = [m.subject for m in mail.outbox]
    assert any("Account deletion requested" in s for s in subjects)
    assert any("received your account deletion request" in s
               for s in subjects)

    assert client.delete("/api/v1/me/deletion-request/").status_code == 204
    assert AccountDeletionRequest.objects.get().status == "cancelled"


def test_officer_cannot_complete_while_money_is_owed(coop, member, secretary):
    _client(member.user, coop).post("/api/v1/me/deletion-request/", {},
                                    format="json")
    req = AccountDeletionRequest.objects.get()

    resp = _client(secretary, coop).post(
        f"/api/v1/deletion-requests/{req.id}/complete/")

    assert resp.status_code == 400
    assert "share capital" in resp.json()["detail"]
    member.refresh_from_db()
    assert member.user.full_name == "Adebayo Okonkwo"


def test_completion_erases_personal_data_and_keeps_the_accounts(
        coop, settled, secretary, dues_type):
    member = settled
    # A contribution, paid out again so nothing is owed: history to keep.
    record_contribution(cooperative=coop, membership=member,
                        contribution_type=dues_type, amount="5000",
                        channel="cash")
    from ledger.services import member_balance
    from savings.models import Withdrawal
    from savings.services import pay_withdrawal
    with use_tenant(coop):
        w = Withdrawal.objects.create(membership=member, amount=Decimal("5000"),
                                      channel="cash")
    pay_withdrawal(w, actor=secretary)
    assert member_balance(member) == 0

    _client(member.user, coop).post("/api/v1/me/deletion-request/", {},
                                    format="json")
    req = AccountDeletionRequest.objects.get()
    officers = _client(secretary, coop)
    listed = officers.get("/api/v1/deletion-requests/").json()["results"]
    assert listed[0]["member_no"] == "IMC-0847"
    assert listed[0]["blockers"] == []

    resp = officers.post(f"/api/v1/deletion-requests/{req.id}/complete/")
    assert resp.status_code == 200, resp.content

    member.refresh_from_db()
    user = member.user
    # Deleted.
    assert user.full_name == "Deleted member"
    assert user.email.endswith("@deleted.invalid")
    assert user.phone == ""
    assert not user.is_active and not user.has_usable_password()
    assert member.address == "" and member.bank_account_no == ""
    assert not MemberDocument.all_objects.filter(membership=member).exists()
    assert member.status == Membership.Status.EXITED
    # Kept: the society's accounts, under the member number.
    assert member.member_no == "IMC-0847"
    assert Contribution.all_objects.filter(membership=member).exists()
    assert LedgerEntry.all_objects.filter(membership=member).exists()
    # And they can no longer sign in.
    assert APIClient().post("/api/v1/auth/token/", {
        "username": "adebayo@example.com", "password": "x"}).status_code != 200


def test_public_form_needs_the_emailed_confirmation(
        coop, member, django_capture_on_commit_callbacks):
    client = APIClient()

    # Same answer for an unknown address: no account enumeration.
    unknown = client.post("/api/v1/public/account-deletion/",
                          {"email": "nobody@example.com"}, format="json")
    known = client.post("/api/v1/public/account-deletion/",
                        {"email": "ADEBAYO@example.com"}, format="json")
    assert unknown.json() == known.json()
    assert len(mail.outbox) == 1
    assert not AccountDeletionRequest.objects.exists()

    assert client.post("/api/v1/public/account-deletion/confirm/",
                       {"token": "forged"}, format="json").status_code == 400

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post("/api/v1/public/account-deletion/confirm/",
                           {"token": deletion_token(member.user)},
                           format="json")
    assert resp.status_code == 200
    req = AccountDeletionRequest.objects.get()
    assert req.source == "web" and req.status == "pending"


def test_a_password_change_kills_an_outstanding_confirm_link(member):
    token = deletion_token(member.user)
    member.user.set_password("new-password")
    member.user.save()

    resp = APIClient().post("/api/v1/public/account-deletion/confirm/",
                            {"token": token}, format="json")
    assert resp.status_code == 400


def test_a_plain_member_cannot_see_or_complete_requests(coop, member):
    client = _client(member.user, coop)
    assert client.get("/api/v1/deletion-requests/").status_code == 403
