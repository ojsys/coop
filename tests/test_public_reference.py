"""The public catalogue behind the signup form.

Signup is a public page, so everything it renders must be reachable without a
token. It used to read the authenticated ``/reference/`` endpoint, which meant
an anonymous visitor got a 401, every dropdown rendered empty, and that same
401 tripped the frontend's response interceptor into redirecting /signup back
to /login. These tests pin both halves of the fix: the catalogue is public,
and ``/reference/`` itself stayed shut.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from tenants.models import Cooperative

pytestmark = pytest.mark.django_db

REFERENCE_URL = "/api/v1/public/reference/"
SUBDIVISIONS_URL = "/api/v1/public/reference/subdivisions/{code}/"
APPLY_URL = "/api/v1/public/apply/"


# ── The catalogue ───────────────────────────────────────────────────────────
def test_catalogue_is_reachable_without_a_token():
    """The whole point: a signed-out visitor can populate the form."""
    resp = APIClient().get(REFERENCE_URL)
    assert resp.status_code == 200, "signup is public, so its catalogue must be"
    body = resp.json()
    assert body["countries"], "an empty country list is the bug this fixes"
    assert body["coop_types"], "an empty type list is the bug this fixes"


def test_making_the_catalogue_public_did_not_open_the_console_endpoint():
    """/reference/ carries deployment detail and must stay authenticated."""
    assert APIClient().get("/api/v1/reference/").status_code in (401, 403)


def test_catalogue_withholds_deployment_detail():
    body = APIClient().get(REFERENCE_URL).json()
    assert "white_label" not in body, "cname_target is not marketing copy"


def test_countries_are_sorted_and_unique():
    countries = APIClient().get(REFERENCE_URL).json()["countries"]
    names = [c["name"] for c in countries]
    assert names == sorted(names), "the dropdown is rendered in payload order"
    codes = [c["code"] for c in countries]
    assert len(codes) == len(set(codes))


def test_each_country_carries_its_own_location_vocabulary():
    """A Kenyan officer should be asked for a County, not a 'State'."""
    countries = {c["code"]: c for c in
                 APIClient().get(REFERENCE_URL).json()["countries"]}

    ng = countries["NG"]
    assert ng["region_label"] == "State"
    assert ng["locality_label"] == "Local Government Area"
    assert ng["has_subdivisions"] is True

    ke = countries["KE"]
    assert ke["region_label"] == "County"
    assert ke["locality_label"] == "Sub-county"


def test_unmapped_country_falls_back_to_neutral_labels():
    """No verified list is not an error — the form degrades to free text."""
    countries = {c["code"]: c for c in
                 APIClient().get(REFERENCE_URL).json()["countries"]}
    fr = countries["FR"]
    assert fr["has_subdivisions"] is False
    assert fr["region_label"] and fr["locality_label"]


# ── Subdivisions ────────────────────────────────────────────────────────────
def test_nigeria_still_has_every_state_and_lga():
    """The Nigerian data survived the move to a country-first structure."""
    resp = APIClient().get(SUBDIVISIONS_URL.format(code="NG"))
    assert resp.status_code == 200
    regions = resp.json()["regions"]
    assert len(regions) == 37, "36 states + FCT"
    assert sum(len(r["localities"]) for r in regions) == 774, "official count"

    lagos = next(r for r in regions if r["region"] == "Lagos")
    assert lagos["capital"] == "Ikeja"
    assert "Ikeja" in lagos["localities"]


def test_subdivisions_are_reachable_without_a_token():
    assert APIClient().get(
        SUBDIVISIONS_URL.format(code="NG")).status_code == 200


def test_country_code_is_case_insensitive():
    lower = APIClient().get(SUBDIVISIONS_URL.format(code="ng")).json()
    upper = APIClient().get(SUBDIVISIONS_URL.format(code="NG")).json()
    assert lower == upper


def test_country_without_data_degrades_instead_of_breaking():
    """A country we have not mapped is a 200 with [], never a 404.

    The frontend reads an empty list as "render text inputs". A 404 would
    surface as an error state and block the form.
    """
    resp = APIClient().get(SUBDIVISIONS_URL.format(code="FR"))
    assert resp.status_code == 200
    assert resp.json()["regions"] == []


def test_nonsense_country_code_is_not_an_error():
    resp = APIClient().get(SUBDIVISIONS_URL.format(code="ZZ"))
    assert resp.status_code == 200
    assert resp.json()["regions"] == []


# ── The application actually stores it ──────────────────────────────────────
def test_application_records_the_country():
    cache.clear()  # the apply endpoint is throttled per scope
    resp = APIClient().post(APPLY_URL, {
        "society_name": "Nairobi Teachers SACCO",
        "applicant_name": "Wanjiru M",
        "applicant_email": "wanjiru@example.com",
        "country": "KE", "state": "Nairobi", "lga": "Westlands",
        "coop_type": "SACCO (Savings & Credit)",
    }, format="json")

    assert resp.status_code == 201, resp.data
    society = Cooperative.objects.get(name="Nairobi Teachers SACCO")
    assert society.country == "KE"
    assert society.state == "Nairobi"
    assert society.lga == "Westlands"


def test_application_without_a_country_is_still_accepted():
    """Country is optional — an incomplete form should not be rejected."""
    cache.clear()
    resp = APIClient().post(APPLY_URL, {
        "society_name": "Unstated Location Coop",
        "applicant_name": "A B",
        "applicant_email": "ab@example.com",
    }, format="json")

    assert resp.status_code == 201, resp.data
    assert Cooperative.objects.get(name="Unstated Location Coop").country == ""
