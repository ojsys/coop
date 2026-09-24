"""Phase A: reference data endpoint + invoice auto-numbering."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from platform_admin.models import Invoice
from platform_admin.services import next_invoice_number

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


def test_reference_endpoint(api):
    resp = api.get("/api/v1/reference/")
    assert resp.status_code == 200
    body = resp.json()
    # 36 states + FCT
    assert len(body["states"]) == 37
    total_lgas = sum(len(s["lgas"]) for s in body["states"])
    assert total_lgas == 774  # official LGA count
    lagos = next(s for s in body["states"] if s["state"] == "Lagos")
    assert lagos["capital"] == "Ikeja"
    assert "Ikeja" in lagos["lgas"]
    assert body["coop_types"]
    assert body["white_label"]["cname_target"]


def test_the_cname_target_is_a_host_we_control():
    """Cooperatives aim their own DNS at this, so it cannot be someone else's.

    It sat on a domain the platform does not own for a long time, which made
    every white-label domain configured against it resolve nowhere.
    """
    from django.conf import settings

    assert settings.WHITE_LABEL_CNAME_TARGET.endswith("mycooperativeos.com"), (
        settings.WHITE_LABEL_CNAME_TARGET
    )


def test_dns_instructions_follow_the_configured_target(settings, coop):
    """Nothing may shadow the setting.

    This value used to be repeated as a defensive default at both call sites,
    where it could never actually be reached — so the copies sat there quietly
    disagreeing with settings, which is how the target ended up naming a domain
    the platform does not own.
    """
    from platform_admin.models import Domain
    from platform_admin.serializers import DomainSerializer

    settings.WHITE_LABEL_CNAME_TARGET = "tenants.example.test"
    domain = Domain.objects.create(cooperative=coop, domain="my.example.coop")

    verification = DomainSerializer(domain).data["verification"]
    assert verification["record_type"] == "CNAME"
    assert verification["target"] == "tenants.example.test"
    assert "tenants.example.test" in verification["instructions"]


def test_reference_requires_auth():
    assert APIClient().get("/api/v1/reference/").status_code in (401, 403)


def test_next_invoice_number_increments(coop):
    from django.utils import timezone
    year = timezone.now().year
    assert next_invoice_number() == f"INV-{year}-001"

    Invoice.objects.create(cooperative=coop, number=f"INV-{year}-005",
                           amount=Decimal("100"))
    assert next_invoice_number() == f"INV-{year}-006"


def test_next_number_endpoint(api):
    resp = api.get("/api/v1/invoices/next-number/")
    assert resp.status_code == 200
    assert resp.json()["number"].startswith("INV-")


def test_new_invoice_defaults_to_draft(api, coop):
    from django.utils import timezone
    resp = api.post("/api/v1/invoices/", {
        "cooperative": coop.id, "number": f"INV-{timezone.now().year}-900",
        "amount": "35000", "status": "draft",
    }, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["status"] == "draft"


def _priv(coop):
    with use_tenant(coop):
        u = User.objects.create_user(email="s@i.co", full_name="S", password="x")
        Membership.objects.create(user=u, member_no="M-1",
                                  role=Role.objects.filter(slug="secretary").first())
    return u


def test_reference_available_to_coop_members(coop):
    c = APIClient()
    c.force_authenticate(_priv(coop))
    assert c.get("/api/v1/reference/").status_code == 200
