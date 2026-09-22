"""
Signup: a society applying to the platform, and a person applying to a society.

Both flows start with a stranger, so the tests lean on the security-relevant
properties: nothing is created ACTIVE without a human decision, no account
exists before approval, and an officer of one society cannot see another's
queue.
"""
from __future__ import annotations

import pytest
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.join_models import JoinRequest
from accounts.join_services import (
    SignupError, approve_join_request, reject_join_request, request_to_join,
)
from accounts.models import Membership, Role, User
from core.context import use_tenant
from platform_admin.models import OnboardingItem
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db

APPLY_URL = "/api/v1/public/apply/"
JOIN_URL = "/api/v1/public/join/"
SOCIETIES_URL = "/api/v1/public/societies/"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def officer(coop):
    """A privileged member who can decide join requests."""
    with use_tenant(coop):
        user = User.objects.create_user(
            email="secretary@imole.coop", full_name="Ngozi Secretary",
            password="x")
        Membership.objects.create(
            user=user, member_no="IMC-0001",
            role=Role.objects.filter(slug="secretary").first())
    return user


@pytest.fixture
def officer_client(coop, officer):
    client = APIClient()
    client.force_authenticate(officer)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


# ── A society applying ──────────────────────────────────────────────────────
def test_application_creates_a_prospective_society_in_the_pipeline():
    resp = APIClient().post(APPLY_URL, {
        "society_name": "Aba Traders Multipurpose",
        "applicant_name": "Chidi Okafor",
        "applicant_email": "chidi@example.com",
        "applicant_phone": "08030000000",
        "state": "Abia", "lga": "Aba North",
        "estimated_members": 320,
    }, format="json")

    assert resp.status_code == 201, resp.data
    society = Cooperative.objects.get(name="Aba Traders Multipurpose")
    # Never ACTIVE from a public form — it goes live only at the end of
    # onboarding, by a human.
    assert society.status == Cooperative.Status.PROSPECTIVE
    assert society.contact_email == "chidi@example.com"
    assert OnboardingItem.objects.filter(cooperative=society).exists()


def test_application_emails_both_the_applicant_and_the_platform(
        django_capture_on_commit_callbacks):
    mail.outbox.clear()
    # Notifications are dispatched through transaction.on_commit, so a mail
    # failure can never roll back an accepted application. That means the test
    # has to let the commit hooks run — pytest's transaction never commits.
    with django_capture_on_commit_callbacks(execute=True):
        APIClient().post(APPLY_URL, {
            "society_name": "Ilorin Farmers", "applicant_name": "Bola A",
            "applicant_email": "bola@example.com",
        }, format="json")

    recipients = [to for m in mail.outbox for to in m.to]
    assert "bola@example.com" in recipients, "the applicant gets an acknowledgement"
    assert len(mail.outbox) == 2, "the platform team is alerted too"


def test_two_societies_with_the_same_name_get_distinct_slugs():
    payload = {"society_name": "Unity Cooperative", "applicant_name": "A",
               "applicant_email": "a@example.com"}
    APIClient().post(APPLY_URL, payload, format="json")
    cache.clear()
    APIClient().post(APPLY_URL, {**payload, "applicant_email": "b@example.com"},
                     format="json")

    slugs = list(Cooperative.objects.filter(name="Unity Cooperative")
                 .values_list("slug", flat=True))
    assert len(slugs) == 2 and len(set(slugs)) == 2, slugs


def test_application_is_throttled():
    payload = {"society_name": "Spam Co", "applicant_name": "S",
               "applicant_email": "s@example.com"}
    client = APIClient()
    codes = [client.post(APPLY_URL, payload, format="json").status_code
             for _ in range(12)]
    assert 429 in codes


# ── Society search ──────────────────────────────────────────────────────────
def test_society_search_needs_a_real_query(coop):
    assert APIClient().get(SOCIETIES_URL).data == []
    assert APIClient().get(SOCIETIES_URL, {"q": "a"}).data == []


def test_society_search_finds_active_societies(coop):
    resp = APIClient().get(SOCIETIES_URL, {"q": coop.name[:4]})
    assert resp.status_code == 200
    assert any(s["slug"] == coop.slug for s in resp.data)


def test_society_search_hides_prospective_societies():
    Cooperative.objects.create(name="Hidden Prospect", slug="hidden-prospect",
                               status=Cooperative.Status.PROSPECTIVE)
    resp = APIClient().get(SOCIETIES_URL, {"q": "Hidden"})
    assert resp.data == [], "a society that cannot admit members must not show"


# ── A person asking to join ─────────────────────────────────────────────────
def test_join_request_creates_no_account(coop):
    resp = APIClient().post(JOIN_URL, {
        "cooperative": coop.id, "full_name": "Tunde Bello",
        "email": "tunde@example.com", "phone": "08031111111",
    }, format="json")

    assert resp.status_code == 201, resp.data
    assert JoinRequest.all_objects.filter(cooperative=coop).count() == 1
    # The crucial property: an unapproved stranger holds no credentials.
    assert not User.objects.filter(email="tunde@example.com").exists()


def test_a_second_pending_request_is_refused(coop):
    payload = {"cooperative": coop.id, "full_name": "Tunde",
               "email": "tunde@example.com"}
    assert APIClient().post(JOIN_URL, payload, format="json").status_code == 201
    second = APIClient().post(JOIN_URL, payload, format="json")

    assert second.status_code == 400
    assert JoinRequest.all_objects.filter(cooperative=coop).count() == 1


def test_an_existing_member_cannot_request_to_join_again(coop, member):
    resp = APIClient().post(JOIN_URL, {
        "cooperative": coop.id, "full_name": member.user.full_name,
        "email": member.user.email,
    }, format="json")

    assert resp.status_code == 400
    # Asserting on behaviour rather than wording: nothing is queued for someone
    # who is already a member, however the message is phrased.
    assert not JoinRequest.all_objects.filter(cooperative=coop).exists()


def test_rejected_applicants_may_apply_again(coop, officer):
    """The duplicate guard covers *pending* requests only."""
    first = request_to_join(cooperative=coop, full_name="Tunde",
                            email="tunde@example.com")
    reject_join_request(first, actor=officer, note="Not yet")

    again = request_to_join(cooperative=coop, full_name="Tunde",
                            email="tunde@example.com")
    assert again.pk != first.pk


# ── Officers deciding ───────────────────────────────────────────────────────
def test_approval_creates_the_member_and_emails_a_set_password_link(
        coop, officer_client, django_capture_on_commit_callbacks):
    joiner = request_to_join(cooperative=coop, full_name="Tunde Bello",
                             email="tunde@example.com")
    mail.outbox.clear()

    # The welcome email is an on_commit hook for the same reason as above.
    with django_capture_on_commit_callbacks(execute=True):
        resp = officer_client.post(
            f"/api/v1/join-requests/{joiner.id}/approve/",
            {"member_no": "IMC-2001"}, format="json")

    assert resp.status_code == 200, resp.data
    joiner.refresh_from_db()
    assert joiner.status == JoinRequest.Status.APPROVED
    assert joiner.membership is not None
    assert joiner.membership.member_no == "IMC-2001"

    user = User.objects.get(email="tunde@example.com")
    assert not user.has_usable_password(), "no password is ever chosen for them"
    assert "/reset-password?uid=" in mail.outbox[0].body


def test_approval_refuses_a_duplicate_member_number(coop, officer_client, member):
    joiner = request_to_join(cooperative=coop, full_name="Tunde",
                             email="tunde@example.com")
    resp = officer_client.post(
        f"/api/v1/join-requests/{joiner.id}/approve/",
        {"member_no": member.member_no}, format="json")

    assert resp.status_code == 400
    joiner.refresh_from_db()
    assert joiner.status == JoinRequest.Status.PENDING


def test_a_decided_request_cannot_be_decided_again(coop, officer):
    joiner = request_to_join(cooperative=coop, full_name="Tunde",
                             email="tunde@example.com")
    reject_join_request(joiner, actor=officer, note="no")

    with pytest.raises(SignupError):
        approve_join_request(joiner, actor=officer, member_no="IMC-9999")


def test_officers_only_see_their_own_society_queue(coop, other_coop,
                                                   officer_client):
    request_to_join(cooperative=coop, full_name="Ours", email="a@example.com")
    request_to_join(cooperative=other_coop, full_name="Theirs",
                    email="b@example.com")

    resp = officer_client.get("/api/v1/join-requests/")
    assert resp.status_code == 200
    names = {r["full_name"] for r in resp.data["results"]}
    assert names == {"Ours"}, "one society's queue must not leak into another's"


def test_an_ordinary_member_cannot_decide_requests(coop, member):
    joiner = request_to_join(cooperative=coop, full_name="Tunde",
                             email="tunde@example.com")
    client = APIClient()
    client.force_authenticate(member.user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))

    resp = client.post(f"/api/v1/join-requests/{joiner.id}/approve/",
                       {"member_no": "IMC-3001"}, format="json")
    assert resp.status_code in (403, 404)
