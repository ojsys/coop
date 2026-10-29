"""
Signup: a society joining the platform, and a person applying to a society.

Both flows start with a stranger, so the tests lean on the security-relevant
properties: a self-serve society is live but unverified (the platform moves no
money for it until KYC is approved), and an officer of one society cannot see
another's queue.

The two flows differ on accounts, deliberately:

* **Asking to join** an existing society creates no account at all until an
  officer approves — an unapproved stranger must never hold credentials to
  somebody else's books.
* **Signing up your own cooperative** creates one with the password chosen on
  the form, so the founder is in the console at once. An address that already
  has an account must give that account's password.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.join_models import JoinRequest
from accounts.join_services import (
    SignupError, approve_join_request, reject_join_request, request_to_join,
)
from accounts.models import Membership, Role, User
from core.context import use_tenant
from platform_admin.models import OnboardingItem, Plan, Subscription
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
def starter_plan():
    return Plan.objects.create(name="Starter", tier=Plan.Tier.SMALL,
                               price_monthly="10000")


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


# ── A society signing up ────────────────────────────────────────────────────
PASSWORD = "Harvest-Season-2026"


def _signup(**overrides):
    payload = {
        "society_name": "Aba Traders Multipurpose",
        "applicant_name": "Chidi Okafor",
        "applicant_email": "chidi@example.com",
        "password": PASSWORD,
        **overrides,
    }
    return APIClient().post(APPLY_URL, payload, format="json")


def test_signup_creates_a_live_society_on_its_free_trial(starter_plan):
    resp = _signup(applicant_phone="08030000000", state="Abia",
                   lga="Aba North", estimated_members=320)

    assert resp.status_code == 201, resp.data
    society = Cooperative.objects.get(name="Aba Traders Multipurpose")
    # Live at once: no pipeline stands between signing up and using it.
    assert society.status == Cooperative.Status.ACTIVE
    assert society.contact_email == "chidi@example.com"
    assert not OnboardingItem.objects.filter(cooperative=society).exists()

    sub = Subscription.objects.get(cooperative=society)
    assert sub.status == Subscription.Status.TRIAL
    assert sub.current_period_end == timezone.localdate() + timedelta(
        days=settings.BILLING_TRIAL_DAYS), "the trial starts at signup"


def test_a_new_society_is_not_yet_verified():
    """Live is not trusted: online payments wait for the KYC review."""
    _signup()
    society = Cooperative.objects.get(name="Aba Traders Multipurpose")
    assert society.verification.status == "not_started"


def test_the_founder_is_chairperson_and_can_sign_in_at_once():
    _signup()
    society = Cooperative.objects.get(name="Aba Traders Multipurpose")
    membership = Membership.all_objects.get(cooperative=society)

    assert membership.user.email == "chidi@example.com"
    assert membership.user.full_name == "Chidi Okafor"
    assert membership.status == Membership.Status.ACTIVE
    assert membership.role.slug == Role.CHAIRPERSON

    login = APIClient().post("/api/v1/auth/token/", {
        "username": "chidi@example.com", "password": PASSWORD,
    }, format="json")
    assert login.status_code == 200, login.data
    assert "token" in login.data


def test_the_founders_email_starts_unconfirmed():
    _signup()
    user = User.objects.get(email="chidi@example.com")
    assert user.email_verified_at is None


def test_a_weak_password_is_refused():
    resp = _signup(password="123")
    assert resp.status_code == 400
    assert "password" in resp.data
    assert not Cooperative.objects.filter(
        name="Aba Traders Multipurpose").exists()


def test_signing_up_with_a_known_email_needs_that_accounts_password(coop):
    """A public form must not let a stranger attach — or reset — an account."""
    existing = User.objects.create_user(
        email="ada@known.test", full_name="Ada", password="Original-Pass-99")

    resp = _signup(applicant_email="ada@known.test", password="Something-Else-1")
    assert resp.status_code == 400
    assert not Cooperative.objects.filter(
        name="Aba Traders Multipurpose").exists()
    existing.refresh_from_db()
    assert existing.check_password("Original-Pass-99"), "password untouched"


def test_an_existing_account_can_found_a_second_society():
    User.objects.create_user(
        email="ada@known.test", full_name="Ada", password="Original-Pass-99")

    resp = _signup(applicant_email="ada@known.test",
                   password="Original-Pass-99")
    assert resp.status_code == 201, resp.data
    society = Cooperative.objects.get(name="Aba Traders Multipurpose")
    assert Membership.all_objects.filter(
        cooperative=society, user__email="ada@known.test").exists()


def test_the_founder_gets_a_member_number():
    _signup(society_name="Ìmọ̀lè Multipurpose Cooperative")

    society = Cooperative.objects.get(name="Ìmọ̀lè Multipurpose Cooperative")
    membership = Membership.all_objects.get(cooperative=society)
    # Initials of the cooperative, diacritics folded away, then a sequence.
    assert membership.member_no == "IMC-0001", membership.member_no


def test_creating_the_founding_admin_twice_is_harmless():
    """The backfill re-runs over cooperatives, so this must be idempotent."""
    from accounts.join_services import create_founding_admin

    _signup(society_name="Idempotent Coop", applicant_email="ngozi@idem.test")
    society = Cooperative.objects.get(name="Idempotent Coop")

    original = Membership.all_objects.get(cooperative=society)
    again = create_founding_admin(
        society, full_name="Ngozi", email="ngozi@idem.test")

    assert again.pk == original.pk, "a second membership was created"
    assert Membership.all_objects.filter(cooperative=society).count() == 1


def test_signup_welcomes_the_founder_and_alerts_the_platform(
        django_capture_on_commit_callbacks):
    mail.outbox.clear()
    # Notifications are dispatched through transaction.on_commit, so a mail
    # failure can never roll back an accepted signup. That means the test has
    # to let the commit hooks run — pytest's transaction never commits.
    with django_capture_on_commit_callbacks(execute=True):
        _signup(society_name="Ilorin Farmers",
                applicant_email="bola@example.com")

    # Two: the founder's welcome and the platform alert. No set-password
    # mail — the founder chose a password on the form.
    subjects = sorted(m.subject for m in mail.outbox)
    assert len(mail.outbox) == 2, subjects
    assert any("New cooperative signup" in s for s in subjects)

    welcome = next(m for m in mail.outbox if "bola@example.com" in m.to)
    assert "Ilorin Farmers is ready" in welcome.subject
    assert "/verify-email?token=" in welcome.body, \
        "the welcome carries the link that confirms the address"


def test_two_societies_with_the_same_name_get_distinct_slugs():
    _signup(society_name="Unity Cooperative", applicant_email="a@example.com")
    cache.clear()
    _signup(society_name="Unity Cooperative", applicant_email="b@example.com")

    slugs = list(Cooperative.objects.filter(name="Unity Cooperative")
                 .values_list("slug", flat=True))
    assert len(slugs) == 2 and len(set(slugs)) == 2, slugs


def test_signup_is_throttled():
    client = APIClient()
    payload = {"society_name": "Spam Co", "applicant_name": "S",
               "applicant_email": "s@example.com", "password": PASSWORD}
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


def test_society_search_finds_a_society_before_its_kyc(unverified_coop):
    """KYC gates money movement only. Hiding unverified societies left every
    society that signed up after the free trial launched unfindable by its own
    members (seen in production)."""
    resp = APIClient().get(SOCIETIES_URL, {"q": "Fresh Start"})
    assert [s["id"] for s in resp.data] == [unverified_coop.id]


def test_society_search_hides_a_society_the_platform_unlisted(coop):
    Cooperative.objects.filter(pk=coop.pk).update(publicly_listed=False)

    resp = APIClient().get(SOCIETIES_URL, {"q": coop.name[:4]})
    assert coop.id not in [s["id"] for s in resp.data]


def test_society_search_finds_a_society_by_a_word_of_what_people_call_it(
        coop):
    """Seen in production: "Digital Adashe" found nothing, because the
    society is registered as "Adashe"."""
    Cooperative.objects.filter(pk=coop.pk).update(name="Adashe")

    resp = APIClient().get(SOCIETIES_URL, {"q": "Digital Adashe"})

    assert [s["id"] for s in resp.data] == [coop.id]


def test_society_search_shows_every_society_sharing_a_name(coop, other_coop):
    """Two societies both called something "Adashe": the member sees both,
    the exact phrase match first, and picks theirs by LGA and state."""
    Cooperative.objects.filter(pk=coop.pk).update(name="Digital Adashe")
    Cooperative.objects.filter(pk=other_coop.pk).update(
        name="Adashe Women Thrift")

    exact = APIClient().get(SOCIETIES_URL, {"q": "Digital Adashe"}).data
    word = APIClient().get(SOCIETIES_URL, {"q": "adashe"}).data

    assert [s["id"] for s in exact] == [coop.id, other_coop.id]
    assert {s["id"] for s in word} == {coop.id, other_coop.id}


def test_society_search_does_not_list_every_cooperative_on_a_generic_word(
        coop):
    """The word fallback skips words almost every name carries, or the search
    would hand out the platform's customer list."""
    Cooperative.objects.filter(pk=coop.pk).update(
        name="Plateau Farmers Cooperative Society")

    resp = APIClient().get(SOCIETIES_URL, {"q": "Digital Cooperative Society"})

    assert resp.data == []


def test_a_self_serve_signup_is_findable_at_once(starter_plan):
    assert _signup(society_name="Lookalike Thrift").status_code == 201

    found = APIClient().get(SOCIETIES_URL, {"q": "Lookalike"}).data
    assert [s["name"] for s in found] == ["Lookalike Thrift"]


def test_only_the_platform_can_change_the_listing(coop):
    """Unlisting is how a look-alike is taken out of strangers' searches, so
    a society's own officers cannot switch it back on for themselves."""
    from tenants.serializers import CooperativeUpdateSerializer

    class _Req:
        user = User(is_platform_admin=False)

    serializer = CooperativeUpdateSerializer(
        coop, data={"publicly_listed": False}, partial=True,
        context={"request": _Req()})
    assert serializer.is_valid(), serializer.errors
    assert "publicly_listed" not in serializer.validated_data


def test_the_migration_lists_existing_societies_but_not_self_serve_ones(
        coop, unverified_coop):
    import importlib

    from django.apps import apps as django_apps

    from audit.models import AuditLog

    AuditLog.all_objects.create(cooperative=unverified_coop,
                                action="cooperative.signup")
    Cooperative.objects.update(publicly_listed=False)

    importlib.import_module(
        "tenants.migrations.0012_publicly_listed").forwards(django_apps, None)

    coop.refresh_from_db()
    unverified_coop.refresh_from_db()
    assert coop.publicly_listed is True
    assert unverified_coop.publicly_listed is False


def test_the_second_migration_lists_the_self_serve_societies_too(
        unverified_coop):
    import importlib

    from django.apps import apps as django_apps

    Cooperative.objects.update(publicly_listed=False)

    importlib.import_module(
        "tenants.migrations.0013_list_every_society").forwards(
            django_apps, None)

    unverified_coop.refresh_from_db()
    assert unverified_coop.publicly_listed is True


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
