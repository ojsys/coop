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
