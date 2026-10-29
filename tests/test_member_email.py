"""
The console's Email page: a society's officers writing to its members.

Pinned: each audience reaches the right members, and only this society's;
a member who opted out of email is never written to; each placeholder is
filled from that member's own record, loan balance included; what an officer
types is never HTML; sending happens once and leaves a per-recipient record;
replies go to the society; and only privileged officers can do any of it.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from communications import member_mailing as mailing
from communications.models import (ChannelPreference, MemberEmail,
                                   MemberEmailRecipient)
from core.context import use_tenant
from loans.models import Loan, LoanProduct, RepaymentInstalment
from tenants.models import Cooperative

pytestmark = pytest.mark.django_db

URL = "/api/v1/member-emails/"


def _person(coop, email, name, role_slug, no, status=Membership.Status.ACTIVE):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=name,
                                        password="x")
        return Membership.objects.create(
            user=user, member_no=no, status=status,
            role=Role.objects.filter(slug=role_slug).first())


def _loan(membership, *, overdue):
    coop = membership.cooperative
    with use_tenant(coop):
        product, _ = LoanProduct.objects.get_or_create(
            name="Emergency", defaults={"interest_rate": Decimal("0"),
                                        "max_amount": Decimal("500000")})
        loan = Loan.objects.create(
            membership=membership, product=product,
            principal=Decimal("120000.00"), interest_rate=Decimal("0"),
            term_months=12, status=Loan.Status.DISBURSED,
            disbursed_at=timezone.now())
        today = timezone.localdate()
        RepaymentInstalment.objects.create(
            loan=loan, sequence=1, amount_due=Decimal("10000.00"),
            due_date=today - timedelta(days=10) if overdue
            else today + timedelta(days=20),
            principal_component=Decimal("10000.00"))
        RepaymentInstalment.objects.create(
            loan=loan, sequence=2, amount_due=Decimal("10000.00"),
            due_date=today + timedelta(days=40),
            principal_component=Decimal("10000.00"))
    return loan


@pytest.fixture
def people(coop, other_coop):
    Cooperative.objects.filter(pk=coop.pk).update(contact_email="hello@imole.test")
    p = {
        "chair": _person(coop, "chair@imole.test", "Ada Chair", "chairperson", "A1"),
        "ade": _person(coop, "ade@imole.test", "Ade Bello", "member", "A2"),
        "bisi": _person(coop, "bisi@imole.test", "Bisi Okon", "member", "A3"),
        "gone": _person(coop, "gone@imole.test", "Gone Away", "member", "A4",
                        status=Membership.Status.SUSPENDED),
        "aba": _person(other_coop, "x@aba.test", "Aba Member", "member", "B1"),
    }
    _loan(p["ade"], overdue=True)
    _loan(p["bisi"], overdue=False)
    _loan(p["gone"], overdue=True)
    return p


def _client(user, coop):
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_COOPERATIVE_ID=str(coop.id))
    return client


@pytest.fixture
def officer(coop, people):
    return _client(people["chair"].user, coop)


def _addresses(coop, **kwargs):
    return {r.address for r in
            mailing.resolve_recipients(coop, **kwargs).recipients}


# ── Who it goes to ─────────────────────────────────────────────────────────
def test_all_means_every_active_member_of_this_society_only(coop, people):
    assert _addresses(coop, audience="all") == {
        "chair@imole.test", "ade@imole.test", "bisi@imole.test"}


def test_a_role(coop, people):
    role = Role.all_objects.get(cooperative=coop, slug="chairperson")
    assert _addresses(coop, audience="role", role_id=role.id) == {
        "chair@imole.test"}
    assert _addresses(coop, audience="role") == set(), "no role, nobody"


def test_borrowers_and_overdue_include_a_suspended_debtor(coop, people):
    assert _addresses(coop, audience="borrowers") == {
        "ade@imole.test", "bisi@imole.test", "gone@imole.test"}
    assert _addresses(coop, audience="overdue") == {
        "ade@imole.test", "gone@imole.test"}


def test_selected_members_never_reach_another_society(coop, people):
    got = _addresses(coop, audience="selected",
                     member_ids=[people["bisi"].id, people["aba"].id])
    assert got == {"bisi@imole.test"}


def test_an_opted_out_member_is_counted_not_written_to(coop, people):
    ChannelPreference.all_objects.create(
        cooperative=coop, membership=people["ade"], email=False)
    resolved = mailing.resolve_recipients(coop, audience="all")
    assert "ade@imole.test" not in {r.address for r in resolved.recipients}
    assert resolved.opted_out == 1


# ── What it says ───────────────────────────────────────────────────────────
def test_loan_placeholders_are_the_members_own(coop, people):
    email = MemberEmail.all_objects.create(
        cooperative=coop, subject="x",
        body="{{first_name}} {{member_no}} {{loan_balance}} "
             "{{amount_overdue}} {{next_due_date}}")
    values = mailing.values_for(email, name="Ade Bello", member_no="A2",
                                membership=people["ade"])
    text = mailing.personalise(email.body, values)
    assert text.startswith("Ade A2 ₦120,000.00 ₦10,000.00 ")
    assert "{{" not in text


def test_without_loan_placeholders_no_loan_lookup_happens(coop, people,
                                                          django_assert_num_queries):
    email = MemberEmail.all_objects.create(cooperative=coop, subject="x",
                                           body="Hello {{name}}")
    email = MemberEmail.all_objects.select_related("cooperative").get(pk=email.pk)
    with django_assert_num_queries(0):
        mailing.values_for(email, name="Ade", membership=people["ade"])


# ── The API ────────────────────────────────────────────────────────────────
def _draft(client, **extra):
    payload = {"purpose": "recovery", "subject": "Overdue, {{first_name}}",
               "body": "You owe {{amount_overdue}}. <b>Pay</b> **now**.",
               "audience": "overdue", **extra}
    res = client.post(URL, payload, format="json")
    assert res.status_code == 201, res.data
    return res.data


def _send_all(client, pk):
    res = client.post(f"{URL}{pk}/send/")
    while res.status_code == 200 and res.data["pending_count"]:
        res = client.post(f"{URL}{pk}/send/")
    return res


def test_send_writes_once_per_member_and_records_it(coop, people, officer):
    draft = _draft(officer)
    mail.outbox.clear()

    res = _send_all(officer, draft["id"])

    assert res.status_code == 200, res.data
    assert res.data["status"] == "sent"
    assert res.data["sent_count"] == 2
    to = sorted(m.to[0] for m in mail.outbox)
    assert to == ["ade@imole.test", "gone@imole.test"]
    ade = next(m for m in mail.outbox if m.to == ["ade@imole.test"])
    assert ade.subject == "Overdue, Ade"
    assert ade.reply_to == ["hello@imole.test"]
    html = ade.alternatives[0][0]
    assert "₦10,000.00" in html
    assert "&lt;b&gt;Pay&lt;/b&gt;" in html and "<strong>now</strong>" in html

    again = officer.post(f"{URL}{draft['id']}/send/")
    assert again.status_code == 200 and len(mail.outbox) == 2, "sent once"
    assert officer.patch(f"{URL}{draft['id']}/", {"subject": "y"},
                         format="json").status_code == 400
    assert officer.delete(f"{URL}{draft['id']}/").status_code == 400


def test_preview_counts_opt_outs(coop, people, officer):
    ChannelPreference.all_objects.create(
        cooperative=coop, membership=people["bisi"], opted_out=True)
    res = officer.post(f"{URL}preview/", {"audience": "all"}, format="json")
    assert res.status_code == 200
    assert res.data["count"] == 2 and res.data["opted_out"] == 1


def test_selected_members_from_another_society_do_not_validate(coop, people,
                                                               officer):
    res = officer.post(URL, {"subject": "s", "body": "b",
                             "audience": "selected",
                             "members": [people["aba"].id]}, format="json")
    assert res.status_code == 400


def test_test_send_goes_to_the_officer(coop, people, officer):
    draft = _draft(officer)
    mail.outbox.clear()
    res = officer.post(f"{URL}{draft['id']}/test/")
    assert res.status_code == 200
    assert [m.to for m in mail.outbox] == [["chair@imole.test"]]
    assert MemberEmail.all_objects.get(pk=draft["id"]).status == "draft"


def test_an_ordinary_member_cannot_see_or_send(coop, people, officer):
    draft = _draft(officer)
    member = _client(people["ade"].user, coop)
    assert member.get(URL).status_code == 403
    assert member.post(f"{URL}{draft['id']}/send/").status_code == 403
    assert member.post(f"{URL}preview/", {"audience": "all"},
                       format="json").status_code == 403


def test_another_societys_officer_cannot_see_the_email(coop, other_coop,
                                                       people, officer):
    draft = _draft(officer)
    aba_chair = _person(other_coop, "chair@aba.test", "Aba Chair",
                        "chairperson", "B2")
    other = _client(aba_chair.user, other_coop)
    assert other.get(URL).data["count"] == 0
    assert other.get(f"{URL}{draft['id']}/").status_code == 404


def test_failed_sends_can_be_retried(coop, people, officer, monkeypatch):
    draft = _draft(officer)
    monkeypatch.setattr(mailing, "send_one", lambda *a, **k: False)
    res = _send_all(officer, draft["id"])
    assert res.data["failed_count"] == 2 and res.data["status"] == "sent"

    monkeypatch.undo()
    assert officer.post(f"{URL}{draft['id']}/retry-failed/").data[
        "pending_count"] == 2
    res = _send_all(officer, draft["id"])
    assert res.data["sent_count"] == 2 and res.data["failed_count"] == 0


def test_the_command_finishes_a_send_left_behind(coop, people, officer):
    draft = _draft(officer)
    email = MemberEmail.all_objects.get(pk=draft["id"])
    mailing.start_sending(email, actor=people["chair"].user)
    mail.outbox.clear()

    call_command("send_member_emails")

    email.refresh_from_db()
    assert email.status == MemberEmail.Status.SENT
    assert len(mail.outbox) == 2
    assert not MemberEmailRecipient.all_objects.filter(
        email=email, status="pending").exists()


def test_the_command_finds_hand_picked_members_without_a_tenant(coop, people,
                                                                officer):
    draft = _draft(officer, audience="selected",
                   members=[people["bisi"].id])
    email = MemberEmail.all_objects.get(pk=draft["id"])
    assert mailing.picked_member_ids(email) == [people["bisi"].id]
