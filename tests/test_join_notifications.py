"""
Who hears about a join request.

Two defects are pinned here. First, a request could go entirely unseen: a
cooperative with no privileged officer had nobody to notify, and nothing said
so. The fix is the published contact address as a fallback — *not* widening the
role check, because JoinRequestViewSet is gated by IsPrivilegedOfficer and a
Chairperson mailed "review it in the console" would only find a permission
wall. Second, the applicant was never acknowledged at all: they had a web page
that vanished when the tab closed and no way to distinguish a submitted request
from a lost one.

``transaction.on_commit`` does not fire inside the test transaction, so every
test here uses ``django_capture_on_commit_callbacks(execute=True)``. Without it
the outbox stays empty and these would pass while proving nothing.
"""
from __future__ import annotations

import pytest
from django.core import mail

from accounts.join_services import request_to_join
from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _holder(coop, slug, email):
    """An active member holding the given role."""
    with use_tenant(coop):
        user = User.objects.create_user(
            email=email, full_name=f"{slug.title()} Person", password="x")
        Membership.objects.create(
            user=user, member_no=f"{slug[:3].upper()}-1",
            role=Role.objects.filter(slug=slug).first())
    return user


def _apply(coop, email="hopeful@example.com"):
    return request_to_join(
        cooperative=coop, full_name="Hopeful Applicant", email=email,
        phone="08030000000", message="I am on the paper register.",
    )


def _to(message):
    return [address.lower() for address in message.to]


def _recipients():
    return {address for m in mail.outbox for address in _to(m)}


# ── The officers ────────────────────────────────────────────────────────────
def test_a_chairperson_is_not_notified(coop,
                                       django_capture_on_commit_callbacks):
    """A Chairperson cannot act on a join request.

    IsPrivilegedOfficer gates the viewset, and Chairperson holds only
    governance/reporting permissions — so mailing them "review it in the
    console" would send them to a permission wall. Notifying people who are
    powerless to respond trains them to ignore the mail.
    """
    _holder(coop, Role.CHAIRPERSON, "chair@imole.coop")
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    assert "chair@imole.coop" not in _recipients()


def test_a_chairperson_led_cooperative_still_reaches_somebody(
        coop, django_capture_on_commit_callbacks):
    """The case the narrow role check depends on being covered.

    No privileged officer, only a Chairperson — so the published contact
    address is the single thing standing between a request and silence.
    """
    _holder(coop, Role.CHAIRPERSON, "chair@imole.coop")
    coop.contact_email = "officers@imole.coop"
    coop.save(update_fields=["contact_email"])
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    assert "officers@imole.coop" in _recipients()


def test_a_privileged_officer_is_still_notified(coop,
                                               django_capture_on_commit_callbacks):
    _holder(coop, Role.SECRETARY, "sec@imole.coop")
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    assert "sec@imole.coop" in _recipients()


def test_the_contact_address_is_the_last_resort(coop,
                                                django_capture_on_commit_callbacks):
    """No office-holder at all — the request must still reach somebody."""
    coop.contact_email = "officers@imole.coop"
    coop.save(update_fields=["contact_email"])
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    assert "officers@imole.coop" in _recipients()


def test_a_plain_member_is_not_notified(coop,
                                        django_capture_on_commit_callbacks):
    """Widening past 'privileged' must not mean mailing the whole register."""
    _holder(coop, Role.MEMBER, "rank@imole.coop")
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    assert "rank@imole.coop" not in _recipients()


def test_nobody_is_mailed_twice(coop, django_capture_on_commit_callbacks):
    """An officer who is also the published contact gets one message."""
    _holder(coop, Role.SECRETARY, "sec@imole.coop")
    coop.contact_email = "SEC@imole.coop"  # same address, different case
    coop.save(update_fields=["contact_email"])
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    officer_mail = [m for m in mail.outbox if "sec@imole.coop" in _to(m)]
    assert len(officer_mail) == 1


# ── The applicant ───────────────────────────────────────────────────────────
def test_the_applicant_is_acknowledged(coop,
                                       django_capture_on_commit_callbacks):
    _holder(coop, Role.SECRETARY, "sec@imole.coop")
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    theirs = [m for m in mail.outbox if "hopeful@example.com" in _to(m)]
    assert len(theirs) == 1
    body = theirs[0].body
    # It must not imply an account already exists, and must say a password is
    # never emailed — the two things applicants get wrong.
    assert "No account exists for you yet" in body
    assert "never be sent a password" in body


def test_the_applicant_is_acknowledged_even_with_nobody_to_notify(
        coop, django_capture_on_commit_callbacks):
    """The regression that matters: the officer path returns early when there
    is nobody to tell, and the applicant must not be caught by that."""
    assert not coop.contact_email
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    assert "hopeful@example.com" in _recipients()


def test_the_acknowledgement_names_the_cooperative(
        coop, django_capture_on_commit_callbacks):
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _apply(coop)

    theirs = [m for m in mail.outbox if "hopeful@example.com" in _to(m)][0]
    assert coop.name in theirs.subject
