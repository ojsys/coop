"""Phase B: provision-first onboarding lifecycle."""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from audit.models import AuditLog
from platform_admin.models import OnboardingItem
from platform_admin.services import start_onboarding
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return User.objects.create_superuser(
        email="admin@startupripple.co", full_name="Platform Admin", password="x")


@pytest.fixture
def api(admin):
    c = APIClient()
    c.force_authenticate(admin)
    return c


def test_provisioning_creates_prospective_tenant_and_onboarding_item(api):
    resp = api.post("/api/v1/cooperatives/", {
        "name": "Kano Millers Union", "slug": "kano-millers",
        "state": "Kano", "coop_type": "Multipurpose", "tier": "small",
    }, format="json")
    assert resp.status_code == 201, resp.content
    coop = Cooperative.objects.get(slug="kano-millers")
    assert coop.status == Cooperative.Status.PROSPECTIVE
    item = OnboardingItem.objects.get(cooperative=coop)
    assert item.stage == OnboardingItem.Stage.DISCOVERY


def test_advance_through_pipeline_activates_at_go_live(api):
    coop = Cooperative.objects.create(
        name="Prospect Co", slug="prospect",
        status=Cooperative.Status.PROSPECTIVE)
    coop.seed_chart_of_accounts()
    item = start_onboarding(coop)

    # Discovery -> Data migration -> Parallel run: still prospective.
    for _ in range(3):
        api.post(f"/api/v1/onboarding-items/{item.id}/advance/")
    item.refresh_from_db()
    coop.refresh_from_db()
    assert item.stage == OnboardingItem.Stage.GO_LIVE
    assert coop.status == Cooperative.Status.ACTIVE  # go-live activates
    assert AuditLog.all_objects.filter(action="cooperative.go_live").exists()


def test_advance_is_idempotent_at_end(api):
    coop = Cooperative.objects.create(
        name="Done Co", slug="done", status=Cooperative.Status.PROSPECTIVE)
    item = start_onboarding(coop)
    for _ in range(8):  # more than enough to reach DONE
        api.post(f"/api/v1/onboarding-items/{item.id}/advance/")
    item.refresh_from_db()
    assert item.stage == OnboardingItem.Stage.DONE
    coop.refresh_from_db()
    assert coop.status == Cooperative.Status.ACTIVE


# ── The go-live announcement ────────────────────────────────────────────────
def _prospect(name="Announce Co", slug="announce", contact="chair@example.com"):
    coop = Cooperative.objects.create(
        name=name, slug=slug, status=Cooperative.Status.PROSPECTIVE,
        contact_email=contact)
    coop.seed_chart_of_accounts()
    return coop, start_onboarding(coop)


def test_going_live_emails_the_cooperative(api, django_capture_on_commit_callbacks):
    """The event that matters most to a customer should not be silent."""
    from django.core import mail

    coop, item = _prospect()
    mail.outbox.clear()

    # Dispatched through transaction.on_commit, which pytest never reaches on
    # its own — the request has to run inside the capture.
    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(3):
            api.post(f"/api/v1/onboarding-items/{item.id}/advance/")

    coop.refresh_from_db()
    item.refresh_from_db()
    assert coop.status == Cooperative.Status.ACTIVE
    assert item.golive_email_sent_at is not None, "the send was not recorded"

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert "chair@example.com" in message.to
    assert coop.name in message.subject


def test_the_announcement_is_not_repeated_on_the_next_advance(
        api, django_capture_on_commit_callbacks):
    """Go-live -> Done must not announce it a second time."""
    from django.core import mail

    coop, item = _prospect(name="Once Co", slug="once-only")
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(8):  # well past Done
            api.post(f"/api/v1/onboarding-items/{item.id}/advance/")

    assert len(mail.outbox) == 1, "the cooperative was told more than once"


def test_the_preview_line_does_not_name_the_cooperative_twice(coop):
    """brand_name is the cooperative's own name, not the platform's.

    Using both in one sentence produced "X is live on X" as the inbox preview
    line — the first thing a recipient reads.
    """
    from django.core import mail

    from communications.email import send_go_live_email

    coop.contact_email = "chair@example.com"
    coop.save(update_fields=["contact_email"])
    mail.outbox.clear()
    assert send_go_live_email(coop) is True

    import re

    html = mail.outbox[0].alternatives[0][0]
    # Target the hidden preheader div specifically. Slicing to the first
    # </div> would also swallow <title>{{ subject }}</title>, and the subject
    # contains the name too — so the test would fail on a correct template.
    match = re.search(r"display:none[^>]*>(.*?)</div>", html, re.S)
    assert match, "preheader div not found in the rendered email"
    preheader = match.group(1)

    assert coop.name in preheader, "the preview line should name the cooperative"
    assert preheader.count(coop.name) == 1, (
        f"named {preheader.count(coop.name)} times in the preview line: "
        f"{preheader.strip()[:120]}")


def test_resending_is_possible_when_the_email_never_arrived():
    """The whole point of the resend: it went out, nobody got it."""
    from django.core import mail

    from platform_admin.services import notify_go_live

    coop, item = _prospect(name="Resend Co", slug="resend")
    assert notify_go_live(item) is True
    first_sent_at = item.golive_email_sent_at
    assert first_sent_at is not None

    mail.outbox.clear()
    assert notify_go_live(item) is False, "should not repeat without force"
    assert mail.outbox == []

    assert notify_go_live(item, force=True) is True
    assert len(mail.outbox) == 1
    item.refresh_from_db()
    assert item.golive_email_sent_at > first_sent_at, "timestamp not refreshed"


def _officer(coop, email, slug="secretary"):
    """A member holding a role that `Role.is_privileged` actually accepts."""
    from accounts.models import Membership, Role, User
    from core.context import use_tenant

    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name=f"Officer {email.split('@')[0]}")
        Membership.objects.create(
            user=user, member_no=f"IMC-{abs(hash(email)) % 9000 + 1000}",
            role=Role.objects.filter(slug=slug).first())
    return user


def test_officers_are_told_as_well_as_the_contact_address(coop):
    """The contact is often the applicant; by go-live the officers matter.

    Privilege comes from permissions, not job title — secretary holds
    members.manage, so it qualifies where a plain member does not.
    """
    from communications.email import cooperative_notification_emails

    coop.contact_email = "contact@example.com"
    coop.save(update_fields=["contact_email"])
    _officer(coop, "secretary@example.com")

    recipients = cooperative_notification_emails(coop)
    assert "contact@example.com" in recipients
    assert "secretary@example.com" in recipients, (
        "a privileged officer was not told their cooperative went live")


def test_an_unprivileged_member_is_not_treated_as_an_officer(coop, member):
    """Chairperson lacks every privileged permission, so it must not qualify.

    A role that reads as senior but carries none of ledger.post,
    ledger.reconcile, members.manage or governance.manage is exactly where
    this could quietly let the wrong people in.
    """
    from communications.email import cooperative_notification_emails

    coop.contact_email = ""
    coop.save(update_fields=["contact_email"])
    _officer(coop, "chair@example.com", slug="chairperson")

    recipients = cooperative_notification_emails(coop)
    assert recipients == [], (
        f"nobody here holds a privileged permission, got {recipients}")


def test_an_officer_who_is_also_the_contact_is_mailed_once(coop):
    from communications.email import cooperative_notification_emails

    _officer(coop, "secretary@example.com")
    coop.contact_email = "Secretary@Example.com"   # same address, different case
    coop.save(update_fields=["contact_email"])

    recipients = cooperative_notification_emails(coop)
    assert len(recipients) == 1, f"mailed twice: {recipients}"


def test_a_cooperative_with_nobody_to_tell_fails_loudly_not_silently():
    """Live with no one informed is a real state, and must be reportable."""
    from platform_admin.services import notify_go_live

    coop, item = _prospect(name="Silent Co", slug="silent", contact="")
    assert notify_go_live(item) is False
    item.refresh_from_db()
    assert item.golive_email_sent_at is None, (
        "nothing was sent, so nothing should be recorded as sent")


def test_start_onboarding_is_idempotent():
    coop = Cooperative.objects.create(name="Once", slug="once")
    a = start_onboarding(coop)
    b = start_onboarding(coop)
    assert a.id == b.id
    assert OnboardingItem.objects.filter(cooperative=coop).count() == 1


def test_seed_provision_default_status_still_active():
    """seed/test path (no status arg) keeps ACTIVE for backward-compat."""
    from tenants.services import provision_cooperative
    coop = provision_cooperative(name="Legacy", slug="legacy")
    assert coop.status == Cooperative.Status.ACTIVE
