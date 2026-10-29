"""
The platform's Email page: the team writing to cooperatives' officers.

Pinned: a message reaches each cooperative's *current* privileged officers
(and its contact address, unless particular officers were picked), never an
ordinary member; an address is written to once per message; what the team
types is never interpreted as HTML; sending happens once and leaves a
per-recipient record; and only platform admins can do any of it.
"""
from __future__ import annotations

import pytest
from django.core import mail
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from platform_admin import mailing
from platform_admin.models import (PlatformEmail, PlatformEmailRecipient,
                                   Subscription)
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db

URL = "/api/v1/platform-emails/"


def _person(coop, email, name, role_slug, no):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=name,
                                        password="x")
        Membership.objects.create(
            user=user, member_no=no,
            role=Role.objects.filter(slug=role_slug).first())
    return user


@pytest.fixture
def people(coop, other_coop):
    Cooperative.objects.filter(pk=coop.pk).update(contact_email="hello@imole.test")
    return {
        "chair": _person(coop, "chair@imole.test", "Ada Chair", "chairperson", "A1"),
        "treasurer": _person(coop, "tre@imole.test", "Bayo Treasurer", "treasurer", "A2"),
        "member": _person(coop, "plain@imole.test", "Chi Member", "member", "A3"),
        "aba_sec": _person(other_coop, "sec@aba.test", "Dayo Sec", "secretary", "B1"),
    }


@pytest.fixture
def admin_client():
    admin = User.objects.create_user(email="ops@platform.test", full_name="Ops",
                                     password="x", is_platform_admin=True)
    client = APIClient()
    client.force_authenticate(admin)
    return client


def _addresses(recipients):
    return {r.address for r in recipients}


# ── Who it goes to ─────────────────────────────────────────────────────────
def test_a_selected_cooperative_means_its_officers_and_contact(coop, people):
    got = mailing.resolve_recipients(audience="selected",
                                     cooperative_ids=[coop.id])

    assert _addresses(got) == {"chair@imole.test", "tre@imole.test",
                               "hello@imole.test"}
    assert "plain@imole.test" not in _addresses(got), "never ordinary members"


def test_every_live_cooperative(coop, other_coop, people):
    got = mailing.resolve_recipients(audience="all",
                                     include_contact_email=False)
    assert _addresses(got) == {"chair@imole.test", "tre@imole.test",
                               "sec@aba.test"}


def test_a_suspended_cooperative_is_left_out(coop, other_coop, people):
    Cooperative.objects.filter(pk=other_coop.pk).update(
        status=Cooperative.Status.SUSPENDED)
    got = mailing.resolve_recipients(audience="all")
    assert "sec@aba.test" not in _addresses(got)


def test_unverified_and_trial_audiences(coop, other_coop, unverified_coop,
                                        people):
    _person(unverified_coop, "new@fresh.test", "Eze New", "chairperson", "C1")
    from platform_admin.models import Plan
    plan = Plan.objects.create(name="Starter", price_monthly="9000")
    for c in (coop, other_coop, unverified_coop):
        Subscription.objects.get_or_create(
            cooperative=c, defaults={"plan": plan,
                                     "status": Subscription.Status.TRIAL})
    Subscription.objects.filter(cooperative=coop).update(
        status=Subscription.Status.ACTIVE)
    Subscription.objects.filter(cooperative=other_coop).update(
        status=Subscription.Status.ACTIVE)

    unverified = mailing.resolve_recipients(audience="unverified")
    trial = mailing.resolve_recipients(audience="trial")

    assert _addresses(unverified) == {"new@fresh.test"}
    assert _addresses(trial) == {"new@fresh.test"}


def test_picking_officers_means_exactly_those_people(coop, people):
    got = mailing.resolve_recipients(
        audience="selected", cooperative_ids=[coop.id],
        only_user_ids=[people["treasurer"].id])
    assert _addresses(got) == {"tre@imole.test"}


def test_an_address_is_written_to_once(coop, other_coop, people):
    """The contact address is often the chair's own; and one person can be an
    officer of two societies."""
    Cooperative.objects.filter(pk=coop.pk).update(contact_email="CHAIR@imole.test")
    with use_tenant(other_coop):
        Membership.objects.create(
            user=people["chair"], member_no="B9",
            role=Role.objects.filter(slug="treasurer").first())

    got = mailing.resolve_recipients(audience="all")

    addresses = [r.address.lower() for r in got]
    assert addresses.count("chair@imole.test") == 1


# ── What it says ───────────────────────────────────────────────────────────
def test_the_body_is_never_html():
    html = mailing.render_body('<script>x</script> **hi** [site](https://a.co)')
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>hi</strong>" in html
    assert 'href="https://a.co"' in html


def test_a_javascript_link_is_not_a_link():
    assert "href" not in mailing.render_body("[x](javascript:alert(1))")


def test_bullets_and_paragraphs():
    html = mailing.render_body("Hello\n\n- one\n- two")
    assert html.count("<li") == 2 and html.startswith("<p")


def test_placeholders_are_filled_per_recipient():
    assert mailing.personalise("Dear {{name}} of {{cooperative}}",
                               name="Ada", cooperative="Imole") == \
        "Dear Ada of Imole"


# ── The API ────────────────────────────────────────────────────────────────
def test_only_platform_admins(coop):
    user = User.objects.create_user(email="x@x.test", full_name="X",
                                    password="x")
    client = APIClient()
    client.force_authenticate(user)
    assert client.get(URL).status_code == 403


def test_compose_preview_send_and_record(admin_client, coop, people):
    mail.outbox.clear()
    preview = admin_client.post(URL + "preview/", {
        "audience": "selected", "cooperatives": [coop.id]}, format="json")
    assert preview.data["count"] == 3

    draft = admin_client.post(URL, {
        "subject": "Hello {{cooperative}}", "body": "Dear {{name}},\n\nNews.",
        "audience": "selected", "cooperatives": [coop.id]}, format="json")
    assert draft.status_code == 201, draft.data

    sent = admin_client.post(f"{URL}{draft.data['id']}/send/")

    assert sent.status_code == 200, sent.data
    assert sent.data["status"] == "sent"
    assert sent.data["sent_count"] == 3 and sent.data["pending_count"] == 0
    assert len(mail.outbox) == 3
    chair = next(m for m in mail.outbox if m.to == ["chair@imole.test"])
    assert chair.subject == f"Hello {coop.name}"
    assert "Dear Ada Chair" in chair.body

    rows = admin_client.get(f"{URL}{draft.data['id']}/recipients/").data
    assert rows["count"] == 3


def test_an_email_is_sent_once(admin_client, coop, people):
    email = PlatformEmail.objects.create(subject="S", body="B",
                                         audience="selected")
    email.cooperatives.set([coop])
    admin_client.post(f"{URL}{email.id}/send/")
    mail.outbox.clear()

    again = admin_client.post(f"{URL}{email.id}/send/")

    assert again.status_code == 200
    assert mail.outbox == [], "a finished send does not go out again"
    assert admin_client.patch(f"{URL}{email.id}/", {"subject": "New"},
                              format="json").status_code == 400
    assert admin_client.delete(f"{URL}{email.id}/").status_code == 400


def test_selected_with_no_cooperative_is_refused(admin_client):
    email = PlatformEmail.objects.create(subject="S", body="B",
                                         audience="selected")
    resp = admin_client.post(f"{URL}{email.id}/send/")
    assert resp.status_code == 400


def test_sending_continues_in_slices(coop, people):
    email = PlatformEmail.objects.create(subject="S", body="B",
                                         audience="selected")
    email.cooperatives.set([coop])
    mailing.start_sending(email, actor=None)

    left = mailing.deliver_pending(email, budget=0)   # one, then out of time

    assert left == 2
    email.refresh_from_db()
    assert email.status == PlatformEmail.Status.SENDING
    assert mailing.deliver_pending(email) == 0
    email.refresh_from_db()
    assert email.status == PlatformEmail.Status.SENT


def test_a_failed_address_can_be_retried(coop, people, monkeypatch):
    email = PlatformEmail.objects.create(subject="S", body="B",
                                         audience="selected")
    email.cooperatives.set([coop])
    mailing.start_sending(email, actor=None)
    monkeypatch.setattr(mailing, "send_one", lambda *a, **k: False)
    mailing.deliver_pending(email)
    assert email.recipients.filter(
        status=PlatformEmailRecipient.Status.FAILED).count() == 3

    monkeypatch.undo()
    assert mailing.retry_failed(email) == 3
    assert mailing.deliver_pending(email) == 0
    assert email.recipients.filter(
        status=PlatformEmailRecipient.Status.SENT).count() == 3


def test_a_test_send_goes_to_the_admin_only(admin_client, coop, people):
    mail.outbox.clear()
    email = PlatformEmail.objects.create(subject="S", body="B",
                                         audience="selected")
    email.cooperatives.set([coop])

    resp = admin_client.post(f"{URL}{email.id}/test/")

    assert resp.status_code == 200
    assert [m.to for m in mail.outbox] == [["ops@platform.test"]]
    email.refresh_from_db()
    assert email.status == PlatformEmail.Status.DRAFT
