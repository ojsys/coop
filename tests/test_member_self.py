"""Member self-service /me/ endpoints — scoping, apply, KYC restrictions."""
from __future__ import annotations

import io
from decimal import Decimal

import pytest
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, User
from core.context import use_tenant
from dividends.services import declare_dividend, post_dividend
from loans.models import Loan, LoanProduct
from savings.models import SavingsProduct

pytestmark = pytest.mark.django_db


def _member(coop, email, member_no, share="10000"):
    with use_tenant(coop):
        u = User.objects.create_user(email=email, full_name=email.split("@")[0],
                                     password="x")
        return Membership.objects.create(user=u, member_no=member_no,
                                         share_capital=Decimal(share))


def _client(membership):
    token, _ = Token.objects.get_or_create(user=membership.user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return c


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (11, 79, 58)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)


def test_profile_get_and_kyc_edit(coop):
    m = _member(coop, "ada@x.co", "M-1")
    client = _client(m)
    assert client.get("/api/v1/me/profile/").status_code == 200

    resp = client.patch("/api/v1/me/profile/", {
        "address": "12 Balogun St", "occupation": "Trader",
        "next_of_kin_name": "Chidi",
    }, format="json")
    assert resp.status_code == 200, resp.content
    m.refresh_from_db()
    assert m.address == "12 Balogun St"
    assert m.occupation == "Trader"


def test_member_cannot_change_officer_fields(coop):
    m = _member(coop, "ada@x.co", "M-1", share="1000")
    client = _client(m)
    client.patch("/api/v1/me/profile/",
                 {"share_capital": "999999", "status": "suspended",
                  "member_no": "HACK"}, format="json")
    m.refresh_from_db()
    assert m.share_capital == Decimal("1000")   # unchanged (read-only)
    assert m.status == Membership.Status.ACTIVE
    assert m.member_no == "M-1"


def test_member_photo_and_document_upload(coop):
    m = _member(coop, "ada@x.co", "M-1")
    client = _client(m)
    photo = SimpleUploadedFile("me.png", _png(), content_type="image/png")
    assert client.patch("/api/v1/me/profile/", {"photo": photo},
                        format="multipart").status_code == 200
    m.refresh_from_db()
    assert m.photo

    doc = SimpleUploadedFile("id.png", _png(), content_type="image/png")
    r = client.post("/api/v1/me/documents/",
                    {"doc_type": "id", "file": doc}, format="multipart")
    assert r.status_code == 201, r.content
    assert client.get("/api/v1/me/documents/").json()["count"] == 1


def test_loan_apply_lands_pending(coop):
    m = _member(coop, "ada@x.co", "M-1")
    with use_tenant(coop):
        product = LoanProduct.objects.create(name="Quick", interest_rate=Decimal("10"))
    r = _client(m).post("/api/v1/me/loans/", {
        "product": product.id, "principal": "50000", "term_months": 6,
        "purpose": "stock",
    }, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["status"] == "pending"
    loan = Loan.all_objects.get(id=r.json()["id"])
    assert loan.membership_id == m.id  # forced to self


def test_member_only_sees_own_records(coop):
    m1 = _member(coop, "a@x.co", "M-1")
    m2 = _member(coop, "b@x.co", "M-2")
    with use_tenant(coop):
        product = LoanProduct.objects.create(name="Q", interest_rate=Decimal("0"))
        Loan.objects.create(membership=m2, product=product, principal=Decimal("1"))
        sp = SavingsProduct.objects.create(name="Ajo")
        from savings.models import SavingsGoal
        SavingsGoal.objects.create(membership=m2, product=sp, name="g",
                                   target_amount=Decimal("100"))

    c1 = _client(m1)
    assert c1.get("/api/v1/me/loans/").json()["count"] == 0        # m2's loan hidden
    assert c1.get("/api/v1/me/savings-goals/").json()["count"] == 0


def test_member_sees_only_own_dividend(coop):
    m1 = _member(coop, "a@x.co", "M-1", share="60000")
    m2 = _member(coop, "b@x.co", "M-2", share="40000")
    with use_tenant(coop):
        d = declare_dividend(cooperative=coop, total_amount="10000",
                             period_label="FY25")
        post_dividend(d)
    rows = _client(m1).get("/api/v1/me/dividends/").json()["results"]
    assert len(rows) == 1
    assert Decimal(rows[0]["amount"]) == Decimal("6000.00")   # m1's 60% only
