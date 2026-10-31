"""
A phone number or an email: either signs in, and at least one is required.

Most members of a farmers' or market traders' society have a phone and no
email. Phones are stored in E.164 so the number a member types at sign-in
matches the one an officer typed, however either of them wrote it.
"""
from __future__ import annotations

import io

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.identity import IdentityError, normalize_phone, payer_email
from accounts.join_services import approve_join_request
from accounts.models import JoinRequest, Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db

LOGIN_URL = "/api/v1/auth/token/"
PASSWORD = "korede-ledger-2026"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()


@pytest.fixture
def officer(coop):
    with use_tenant(coop):
        user = User.objects.create_user(email="sec@imole.test",
                                        full_name="Sec", password="x")
        return Membership.objects.create(
            user=user, member_no="IMC-0005",
            role=Role.objects.get(slug="secretary"))


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


def _login(username, password=PASSWORD):
    return APIClient().post(LOGIN_URL, {"username": username,
                                        "password": password}, format="json")


# ── Normalising ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed", [
    "08034412290", "0803 441 2290", "+234 803 441 2290", "2348034412290",
    "0803-441-2290",
])
def test_every_way_of_writing_a_nigerian_number_is_the_same(typed):
    assert normalize_phone(typed) == "+2348034412290"


def test_a_number_from_another_country_keeps_its_code():
    assert normalize_phone("0712 345678", "KE") == "+254712345678"


def test_a_non_number_is_refused():
    with pytest.raises(IdentityError):
        normalize_phone("12345")


def test_blank_is_no_phone():
    assert normalize_phone("  ") is None


# ── The account ─────────────────────────────────────────────────────────────
def test_an_account_can_have_a_phone_and_no_email():
    user = User.objects.create_user(phone="08034412290", full_name="Musa")
    assert user.email is None
    assert user.phone == "+2348034412290"


def test_an_account_needs_a_phone_or_an_email():
    with pytest.raises(ValueError):
        User.objects.create_user(full_name="Nobody")


def test_two_phone_only_accounts_do_not_clash_on_a_blank_email():
    User.objects.create_user(phone="08034412290", full_name="A")
    User.objects.create_user(phone="08055501234", full_name="B")
    assert User.objects.filter(email=None).count() == 2


def test_the_api_shows_a_missing_email_as_blank(coop):
    """Installed app builds read email as a required string."""
    user = User.objects.create_user(phone="08034412290", full_name="Musa",
                                    password=PASSWORD)
    with use_tenant(coop):
        Membership.objects.create(user=user, member_no="IMC-0001")
    me = _client(user, coop).get("/api/v1/me/").json()
    assert me["email"] == ""
    assert me["phone"] == "+2348034412290"


# ── Signing in ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("typed", ["08034412290", "+2348034412290",
                                   "0803 441 2290"])
def test_sign_in_with_the_phone_however_it_is_typed(typed):
    User.objects.create_user(phone="08034412290", full_name="Musa",
                             password=PASSWORD)
    resp = _login(typed)
    assert resp.status_code == 200, resp.content
    assert resp.json()["token"]


def test_sign_in_with_the_email_still_works_in_any_case():
    User.objects.create_user(email="ada@example.com", phone="08034412290",
                             full_name="Ada", password=PASSWORD)
    assert _login("ADA@example.com").status_code == 200
    assert _login("08034412290").status_code == 200


def test_a_wrong_password_on_a_phone_is_refused_like_any_other():
    User.objects.create_user(phone="08034412290", full_name="Musa",
                             password=PASSWORD)
    known = _login("08034412290", "wrong")
    cache.clear()
    unknown = _login("08099999999", "wrong")
    assert known.status_code == unknown.status_code == 400
    assert known.json() == unknown.json()


def test_an_inactive_account_cannot_sign_in_by_phone():
    User.objects.create_user(phone="08034412290", full_name="Musa",
                             password=PASSWORD, is_active=False)
    assert _login("08034412290").status_code == 400


# ── Officers adding people ──────────────────────────────────────────────────
def test_an_officer_adds_a_member_with_only_a_phone(coop, officer):
    resp = _client(officer.user, coop).post("/api/v1/members/", {
        "full_name": "Musa Bello", "phone": "0803 441 2290"}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["email"] == ""
    assert resp.json()["phone"] == "+2348034412290"
    assert User.objects.get(phone="+2348034412290").email is None


def test_adding_a_member_needs_a_phone_or_an_email(coop, officer):
    resp = _client(officer.user, coop).post("/api/v1/members/", {
        "full_name": "Musa Bello"}, format="json")
    assert resp.status_code == 400
    assert "phone" in resp.json()


def test_an_invalid_phone_is_refused(coop, officer):
    resp = _client(officer.user, coop).post("/api/v1/members/", {
        "full_name": "Musa Bello", "phone": "12345"}, format="json")
    assert resp.status_code == 400
    assert "phone" in resp.json()


def test_a_phone_already_on_an_account_adds_that_person(coop, officer):
    """The same person joining a second society keeps one account."""
    existing = User.objects.create_user(phone="08034412290", full_name="Musa")
    resp = _client(officer.user, coop).post("/api/v1/members/", {
        "full_name": "Musa B.", "phone": "+2348034412290"}, format="json")
    assert resp.status_code == 201, resp.content
    assert Membership.all_objects.get(pk=resp.json()["id"]).user == existing


def test_a_phone_and_email_of_two_different_people_is_refused(coop, officer):
    User.objects.create_user(phone="08034412290", full_name="Musa")
    User.objects.create_user(email="ada@example.com", full_name="Ada")
    resp = _client(officer.user, coop).post("/api/v1/members/", {
        "full_name": "Who", "phone": "08034412290",
        "email": "ada@example.com"}, format="json")
    assert resp.status_code == 400
    assert "two different accounts" in str(resp.json())


# ── Officers editing people ─────────────────────────────────────────────────
@pytest.fixture
def phone_member(coop):
    with use_tenant(coop):
        user = User.objects.create_user(phone="08034412290", full_name="Musa")
        return Membership.objects.create(user=user, member_no="IMC-0010")


def test_an_officer_corrects_a_members_phone(coop, officer, phone_member):
    resp = _client(officer.user, coop).patch(
        f"/api/v1/members/{phone_member.pk}/", {"phone": "08055501234"},
        format="json")
    assert resp.status_code == 200, resp.content
    phone_member.user.refresh_from_db()
    assert phone_member.user.phone == "+2348055501234"


def test_an_officer_adds_an_email_where_there_was_none(coop, officer,
                                                       phone_member):
    resp = _client(officer.user, coop).patch(
        f"/api/v1/members/{phone_member.pk}/", {"email": "musa@example.com"},
        format="json")
    assert resp.status_code == 200, resp.content
    phone_member.user.refresh_from_db()
    assert phone_member.user.email == "musa@example.com"


def test_an_existing_email_is_never_replaced(coop, officer):
    with use_tenant(coop):
        user = User.objects.create_user(email="ada@example.com",
                                        full_name="Ada")
        member = Membership.objects.create(user=user, member_no="IMC-0011")
    _client(officer.user, coop).patch(
        f"/api/v1/members/{member.pk}/", {"email": "other@example.com"},
        format="json")
    user.refresh_from_db()
    assert user.email == "ada@example.com"


def test_a_phone_cannot_be_moved_onto_someone_elses(coop, officer,
                                                    phone_member):
    User.objects.create_user(phone="08055501234", full_name="Other")
    resp = _client(officer.user, coop).patch(
        f"/api/v1/members/{phone_member.pk}/", {"phone": "08055501234"},
        format="json")
    assert resp.status_code == 400
    assert "another account" in str(resp.json())


def test_a_phone_only_member_cannot_lose_their_phone(coop, officer,
                                                     phone_member):
    resp = _client(officer.user, coop).patch(
        f"/api/v1/members/{phone_member.pk}/", {"phone": ""}, format="json")
    assert resp.status_code == 400


def test_an_officer_cannot_change_how_a_member_of_another_society_signs_in(
        coop, other_coop, officer, phone_member):
    with use_tenant(other_coop):
        Membership.objects.create(user=phone_member.user, member_no="X-1")
    resp = _client(officer.user, coop).patch(
        f"/api/v1/members/{phone_member.pk}/", {"phone": "08055501234"},
        format="json")
    assert resp.status_code == 400
    assert "only they can change" in str(resp.json())


def test_a_member_changes_their_own_phone(coop, phone_member, other_coop):
    """Even when they belong to two societies: it is their account."""
    with use_tenant(other_coop):
        Membership.objects.create(user=phone_member.user, member_no="X-1")
    resp = _client(phone_member.user, coop).patch(
        "/api/v1/me/profile/", {"phone": "08055501234"}, format="json")
    assert resp.status_code == 200, resp.content
    phone_member.user.refresh_from_db()
    assert phone_member.user.phone == "+2348055501234"


# ── CSV import ──────────────────────────────────────────────────────────────
def test_import_takes_rows_with_a_phone_or_an_email(coop, officer):
    csv = ("full_name,phone,email\n"
           "Musa Bello,0803 441 2290,\n"
           "Ada Obi,,ada@example.com\n"
           "Nobody,,\n"
           "Bad Phone,12345,\n")
    upload = io.BytesIO(csv.encode())
    upload.name = "members.csv"
    resp = _client(officer.user, coop).post(
        "/api/v1/members/import/", {"file": upload}, format="multipart")
    body = resp.json()
    assert body["created"] == 2, body
    assert [e["row"] for e in body["errors"]] == [4, 5]
    assert User.objects.get(phone="+2348034412290").email is None


# ── Joining ─────────────────────────────────────────────────────────────────
def test_a_person_asks_to_join_with_only_a_phone(coop):
    resp = APIClient().post("/api/v1/public/join/", {
        "cooperative": coop.id, "full_name": "Musa Bello",
        "phone": "0803 441 2290"}, format="json")
    assert resp.status_code == 201, resp.content
    request = JoinRequest.all_objects.get()
    assert request.phone == "+2348034412290"
    assert request.email == ""


def test_a_join_request_needs_a_phone_or_an_email(coop):
    resp = APIClient().post("/api/v1/public/join/", {
        "cooperative": coop.id, "full_name": "Musa Bello"}, format="json")
    assert resp.status_code == 400


def test_a_second_request_from_the_same_phone_is_refused(coop):
    for typed, expected in (("08034412290", 201), ("+234 803 441 2290", 400)):
        resp = APIClient().post("/api/v1/public/join/", {
            "cooperative": coop.id, "full_name": "Musa", "phone": typed},
            format="json")
        assert resp.status_code == expected, resp.content


def test_approving_a_phone_only_request_creates_a_phone_only_account(
        coop, officer):
    with use_tenant(coop):
        request = JoinRequest.objects.create(
            full_name="Musa Bello", phone="+2348034412290")
    membership = approve_join_request(request, actor=officer.user)
    assert membership.user.phone == "+2348034412290"
    assert membership.user.email is None
    assert not membership.user.has_usable_password()


def test_a_phone_only_member_sees_their_own_join_requests(coop, other_coop):
    user = User.objects.create_user(phone="08034412290", full_name="Musa",
                                    password=PASSWORD)
    with use_tenant(coop):
        Membership.objects.create(user=user, member_no="IMC-0001")
    client = _client(user, coop)
    resp = client.post("/api/v1/me/join-requests/",
                       {"cooperative": other_coop.id}, format="json")
    assert resp.status_code == 201, resp.content
    assert len(client.get("/api/v1/me/join-requests/").json()) == 1


# ── Paying ──────────────────────────────────────────────────────────────────
def test_paystack_gets_a_stand_in_email_for_a_phone_only_member(settings):
    settings.PAYER_EMAIL_DOMAIN = "members.example.test"
    user = User.objects.create_user(phone="08034412290", full_name="Musa")
    assert payer_email(user) == f"member-{user.pk}@members.example.test"
    with_email = User.objects.create_user(email="ada@example.com",
                                          full_name="Ada")
    assert payer_email(with_email) == "ada@example.com"
