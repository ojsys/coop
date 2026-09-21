"""Phase F: platform profile, notification templates, plans mgmt, export."""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from platform_admin.models import NotificationTemplate, PlatformProfile

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


def test_platform_profile_get_and_patch(api):
    resp = api.get("/api/v1/platform/profile/")
    assert resp.status_code == 200
    assert "name" in resp.json()

    resp = api.patch("/api/v1/platform/profile/",
                     {"name": "Ripple Co", "support_email": "help@x.co"},
                     format="json")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Ripple Co"
    assert PlatformProfile.load().name == "Ripple Co"


def test_notification_template_crud_and_preview(api):
    resp = api.post("/api/v1/notification-templates/", {
        "key": "welcome", "name": "Welcome", "channel": "email",
        "subject": "Hi {{coop_name}}", "body": "Welcome {{coop_name}} — {{amount}}",
    }, format="json")
    assert resp.status_code == 201, resp.content
    tid = resp.json()["id"]

    resp = api.post(f"/api/v1/notification-templates/{tid}/preview/")
    assert resp.status_code == 200
    body = resp.json()
    assert "{{coop_name}}" not in body["body"]  # placeholders rendered
    assert "{{coop_name}}" not in body["subject"]


def test_plan_management(api):
    resp = api.post("/api/v1/plans/", {
        "name": "Enterprise", "tier": "large", "price_monthly": "250000",
    }, format="json")
    assert resp.status_code == 201, resp.content
    pid = resp.json()["id"]
    resp = api.patch(f"/api/v1/plans/{pid}/", {"price_monthly": "300000"},
                     format="json")
    assert resp.status_code == 200
    assert Decimal(resp.json()["price_monthly"]) == Decimal("300000")


def test_platform_export_csv(api, coop):
    resp = api.get("/api/v1/platform/export/")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    header = resp.content.decode().splitlines()[0]
    assert header.startswith("id,name,slug,state")


def test_settings_reject_non_admin(coop):
    from accounts.models import Membership, Role
    from core.context import use_tenant
    with use_tenant(coop):
        u = User.objects.create_user(email="m@i.co", full_name="M", password="x")
        Membership.objects.create(user=u, member_no="M-1",
                                  role=Role.objects.filter(slug="member").first())
    c = APIClient()
    c.force_authenticate(u)
    assert c.get("/api/v1/platform/profile/").status_code == 403
    assert c.get("/api/v1/notification-templates/").status_code == 403
    assert c.get("/api/v1/platform/export/").status_code == 403
