"""
A society's invite link: /join/<code> takes a member straight to its join form.

The code is the officers' to choose, so most of what is pinned here is about
that choice: it must stay unique and link-safe, only officers may change it,
and switching the link off (or the society leaving ACTIVE) must stop it
resolving without telling a visitor which of those happened.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from tenants.models import Cooperative
from tenants.services import provision_cooperative

pytestmark = pytest.mark.django_db


def _link(code):
    return f"/api/v1/public/join/{code}/"


def _client_for(coop, role_slug, email):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name="Someone",
                                        password="x")
        Membership.objects.create(
            user=user, member_no=f"M-{role_slug[:3]}",
            role=Role.objects.filter(slug=role_slug).first())
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def officer_client(coop):
    return _client_for(coop, "secretary", "secretary@imole.coop")


def test_a_new_society_gets_a_link_from_its_slug(coop):
    assert coop.join_code == "imole"
    assert coop.join_link_enabled is True


def test_a_slug_another_society_chose_as_its_code_is_not_reused(coop):
    coop.join_code = "aba"
    coop.save()

    other = provision_cooperative(name="Aba Traders Cooperative", slug="aba")

    assert other.join_code == "aba-2"


def test_the_link_resolves_to_the_society(coop):
    response = APIClient().get(_link("IMOLE"))

    assert response.status_code == 200
    assert response.data["id"] == coop.id
    assert response.data["name"] == coop.name
    # Recognition, not a profile: nothing operational leaks through.
    assert "bank_account_no" not in response.data
    assert "member_cap" not in response.data


@pytest.mark.parametrize("change", [
    {"join_link_enabled": False},
    {"status": Cooperative.Status.SUSPENDED},
    {"join_code": "renamed"},
])
def test_a_dead_link_is_indistinguishable_from_an_unknown_one(coop, change):
    for field, value in change.items():
        setattr(coop, field, value)
    coop.save()

    assert APIClient().get(_link("imole")).status_code == 404


def test_an_officer_customises_the_link(coop, officer_client):
    response = officer_client.patch(f"/api/v1/cooperatives/{coop.id}/",
                                    {"join_code": "Imole-Farmers"},
                                    format="json")

    assert response.status_code == 200, response.data
    coop.refresh_from_db()
    assert coop.join_code == "imole-farmers"
    assert APIClient().get(_link("imole-farmers")).status_code == 200


@pytest.mark.parametrize("code", [
    "ab", "has space", "under_score", "-leading", "double--hyphen", "",
    "x" * 51,
])
def test_a_code_that_would_not_survive_a_url_is_refused(coop, officer_client,
                                                        code):
    response = officer_client.patch(f"/api/v1/cooperatives/{coop.id}/",
                                    {"join_code": code}, format="json")

    assert response.status_code == 400
    assert "join_code" in response.data


def test_another_societys_code_is_refused(coop, other_coop, officer_client):
    response = officer_client.patch(f"/api/v1/cooperatives/{coop.id}/",
                                    {"join_code": other_coop.join_code.upper()},
                                    format="json")

    assert response.status_code == 400
    assert "join_code" in response.data


def test_a_plain_member_cannot_change_the_link(coop):
    client = _client_for(coop, "member", "plain@imole.coop")

    response = client.patch(f"/api/v1/cooperatives/{coop.id}/",
                            {"join_code": "hijacked"}, format="json")

    assert response.status_code == 403
    coop.refresh_from_db()
    assert coop.join_code == "imole"
