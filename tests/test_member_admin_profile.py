"""
Editing a member's whole profile from one admin page.

Staff get asked to "change this member's phone number" or "fix their bank
details". That used to be impossible from the member's own page, for two reasons:

* **A profile spans two tables.** Name and phone live on ``User`` — one identity,
  possibly several cooperatives — while everything else lives on ``Membership``.
  Staff had to know to go to Users instead.
* **``bank_code`` was not on the form at all**, so the one field that decides
  whether a member can be paid electronically could not be set here. The same
  omission existed in the web console's member form.

Email stays read-only on this page on purpose: it is the login identity, and
changing it changes how someone signs in. That belongs on the user record, as a
deliberate act rather than a side effect of correcting an address.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from accounts.admin import MembershipAdmin, MembershipAdminForm
from accounts.models import Membership, User
from core.context import use_tenant
from payments.models import Bank

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    return User.objects.create_user(
        email="ops@startupripple.co", full_name="Ops", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=True)


def _request(user):
    request = RequestFactory().post("/admin/")
    request.user = user
    request._messages = type("_S", (), {"add": lambda self, *a, **k: None})()
    return request


def _form_data(member, **overrides):
    data = {
        "cooperative": member.cooperative_id,
        "user": member.user_id,
        "member_no": member.member_no,
        "role": member.role_id or "",
        "status": member.status,
        "full_name": member.user.full_name,
        "phone": member.user.phone,
        "share_capital": str(member.share_capital),
        "joined_at": "",
        "exited_at": "",
        "occupation": member.occupation,
        "bank_name": member.bank_name,
        "bank_code": member.bank_code,
        "bank_account_no": member.bank_account_no,
        "date_of_birth": "",
        "gender": "",
        "address": member.address,
        "next_of_kin_name": member.next_of_kin_name,
        "next_of_kin_phone": member.next_of_kin_phone,
        "documents-TOTAL_FORMS": "0",
        "documents-INITIAL_FORMS": "0",
        "documents-MIN_NUM_FORMS": "0",
        "documents-MAX_NUM_FORMS": "1000",
    }
    data.update(overrides)
    return data


def _bound(member, **overrides):
    """A bound form, built inside the tenant.

    ``fields = "__all__"`` builds the form's fields once, at class-definition
    time — when no tenant is bound. Role is tenant-scoped, so its queryset is
    baked empty and every role choice is rejected, whatever is bound later. The
    real admin never hits this because UnscopedAdminMixin replaces each FK's
    queryset per request; a test constructing the form directly has to do the
    same.
    """
    from accounts.models import Role

    with use_tenant(member.cooperative):
        form = MembershipAdminForm(
            instance=member, data=_form_data(member, **overrides))
        form.fields["role"].queryset = Role.all_objects.all()
        assert form.is_valid(), form.errors
        form.save()
    return form


# ── Every profile field is present ──────────────────────────────────────────
def test_every_editable_membership_field_is_on_the_form(member):
    """A field the model stores but the admin omits is a field staff cannot help
    with — which is how bank_code went missing."""
    form = MembershipAdminForm(instance=member)

    editable = {
        f.name for f in Membership._meta.fields
        if f.editable and not f.auto_created and f.name not in {"id"}
    }
    missing = editable - set(form.fields)

    assert not missing, f"not editable in the admin: {sorted(missing)}"


def test_the_bank_code_is_on_the_form(member):
    """Named explicitly: it is the field that decides whether a member can be
    paid, and it was the one missing."""
    assert "bank_code" in MembershipAdminForm(instance=member).fields


def test_the_persons_name_and_phone_are_on_the_form(member):
    form = MembershipAdminForm(instance=member)

    assert form.fields["full_name"].initial == member.user.full_name
    assert "phone" in form.fields


# ── Saving writes through to the user record ─────────────────────────────────
def test_changing_the_name_updates_the_user_record(member):
    _bound(member, full_name="Adebayo A. Okonkwo")

    member.user.refresh_from_db()
    assert member.user.full_name == "Adebayo A. Okonkwo"


def test_changing_the_phone_updates_the_user_record(member):
    _bound(member, phone="08030000001")

    member.user.refresh_from_db()
    assert member.user.phone == "08030000001"


def test_an_unchanged_person_is_not_written_to(member):
    """So two officers editing different memberships of the same person cannot
    clobber each other's unrelated edits."""
    before = member.user.updated_at

    _bound(member)

    member.user.refresh_from_db()
    assert member.user.updated_at == before


def test_the_email_is_not_editable_here(member):
    """It is the login identity; changing it changes how someone signs in."""
    form = MembershipAdminForm(instance=member)

    assert "email" not in form.fields


def test_the_page_links_to_the_user_record_for_the_email(member, operator):
    rendered = MembershipAdmin(Membership, AdminSite()).email_link(member)

    assert member.user.email in rendered
    assert f"/admin/accounts/user/{member.user_id}/change/" in rendered


# ── The bank code is chosen, not typed ──────────────────────────────────────
def test_the_bank_code_is_a_dropdown_once_the_catalogue_is_loaded(member):
    """A typed code does not fail visibly — it points a payout at the wrong
    bank."""
    Bank.objects.create(code="044", name="Access Bank", slug="access",
                        currency="NGN", active=True)
    Bank.objects.create(code="058", name="GTBank", slug="gtb",
                        currency="NGN", active=True)

    field = MembershipAdminForm(instance=member).fields["bank_code"]

    labels = dict(field.choices)
    assert "Access Bank (044)" in labels.values()
    assert "GTBank (058)" in labels.values()


def test_an_empty_catalogue_falls_back_and_says_how_to_fix_it(member):
    """Offering an empty dropdown would be a dead end; the fallback explains
    itself instead."""
    assert Bank.objects.count() == 0

    field = MembershipAdminForm(instance=member).fields["bank_code"]

    assert not isinstance(field, type(None))
    assert "has not been loaded" in field.help_text
    assert "Refresh the catalogue" in field.help_text


def test_a_code_dropped_by_the_provider_is_kept_as_a_choice(member):
    """Otherwise it vanishes from the dropdown and is silently cleared on the
    next save, quietly making a payable member unpayable."""
    member.bank_code = "999"
    member.save(update_fields=["bank_code"])
    Bank.objects.create(code="044", name="Access Bank", slug="access",
                        currency="NGN", active=True)

    choices = dict(MembershipAdminForm(instance=member).fields["bank_code"].choices)

    assert "999" in choices
    assert "no longer in the catalogue" in choices["999"]


def test_a_chosen_bank_code_is_saved(member):
    Bank.objects.create(code="044", name="Access Bank", slug="access",
                        currency="NGN", active=True)

    _bound(member, bank_code="044", bank_name="Access Bank",
           bank_account_no="0123456789")

    member.refresh_from_db()
    assert member.bank_code == "044"


def test_saving_a_code_makes_the_member_payable(member):
    """What the whole field is for."""
    from loans.models import Loan, LoanProduct

    Bank.objects.create(code="044", name="Access Bank", slug="access",
                        currency="NGN", active=True)
    _bound(member, bank_code="044", bank_name="Access Bank",
           bank_account_no="0123456789")
    member.refresh_from_db()

    with use_tenant(member.cooperative):
        product = LoanProduct.objects.create(
            name="Quick", interest_rate=Decimal("10"),
            max_amount=Decimal("500000"), max_term_months=12)
        loan = Loan.objects.create(
            membership=member, product=product, principal=Decimal("50000"),
            interest_rate=Decimal("10"), term_months=6)
    loan.snapshot_destination()

    assert loan.destination_is_payable is True


# ── The page still renders, and still needs 2FA ─────────────────────────────
def test_the_member_page_renders(client, member, operator):
    client.force_login(operator)

    resp = client.get(f"/admin/accounts/membership/{member.pk}/change/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Payout destination" in body
    assert "Person" in body


def test_the_member_page_is_read_only_without_2fa(client, member):
    plain = User.objects.create_user(
        email="no2fa@startupripple.co", full_name="No 2FA", password="x",
        is_staff=True, is_superuser=True, two_factor_enabled=False)
    client.force_login(plain)

    body = client.get(
        f"/admin/accounts/membership/{member.pk}/change/").content.decode()

    assert 'name="_save"' not in body
    assert "two-factor authentication is not enabled" in body
