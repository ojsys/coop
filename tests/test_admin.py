"""
Admin tests.

These guard the properties that are easy to break silently:

* every project model stays registered;
* tenant-scoped admins read through ``all_objects``, so changelists are not
  empty when no tenant is bound (which is every admin request);
* every admin page actually renders, against a row of real data;
* append-only records cannot be written;
* the admin is restricted to platform admins, and money-touching models require
  2FA;
* derived columns do not issue a query per row.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.models import JoinRequest, MemberDocument, Membership, User
from approvals.models import ApprovalRequest
from audit.models import AuditLog
from communications.models import Announcement, ChannelPreference, Notification
from contributions.models import Contribution
from contributions.services import record_contribution
from core.context import set_current_cooperative, use_tenant
from dividends.models import DividendAllocation, DividendDeclaration
from governance.models import Attendance, Meeting, Resolution, Vote
from ledger.models import Account, Journal, LedgerEntry
from loans.models import Loan, LoanProduct, LoanRepayment, RepaymentInstalment
from payments.models import PaymentEvent, Provider, ProviderAccount
from platform_admin.models import (Domain, Incident, Invoice,
                                   NotificationTemplate, OnboardingItem, Plan,
                                   PlatformProfile, PlatformTeamMember,
                                   ProviderCheck, ProviderStatus, Subscription,
                                   SupportTicket)
from savings.models import SavingsGoal, SavingsProduct
from tenants.models import Cooperative

# Apps whose models the admin is expected to cover.
PROJECT_APPS = {
    "tenants", "accounts", "ledger", "contributions", "payments", "audit",
    "governance", "communications", "reports", "platform_admin", "savings",
    "dividends", "loans", "approvals",
}


def project_models():
    return [m for m in apps.get_models() if m._meta.app_label in PROJECT_APPS]


@pytest.fixture
def operator(db):
    """A platform admin with 2FA on — what a real admin user looks like."""
    return User.objects.create_superuser(
        email="ops@startupripple.co", password="pw-test-12345",
        full_name="Platform Operator", two_factor_enabled=True,
    )


@pytest.fixture
def admin_client_2fa(client, operator):
    client.force_login(operator)
    return client


@pytest.fixture
def admin_request(operator):
    """A request shaped like a real admin one: a user, and message storage.

    Admin actions report results via ``message_user``, which needs the message
    middleware a bare RequestFactory request doesn't run.
    """
    request = RequestFactory().get("/admin/")
    request.user = operator
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@pytest.fixture
def dataset(coop, member, dues_type, member_funds):
    """At least one row for every model, so every change page can render."""
    now = timezone.now()

    # A real contribution → also creates a Journal + LedgerEntry rows.
    contribution = record_contribution(
        cooperative=coop, membership=member, contribution_type=dues_type,
        amount=Decimal("5000.00"), channel="cash", occurred_at=now,
    )

    with use_tenant(coop):
        MemberDocument.objects.create(
            membership=member, doc_type=MemberDocument.DocType.ID,
            label="NIN slip", file="member_documents/nin.pdf",
        )
        ChannelPreference.objects.create(membership=member)
        announcement = Announcement.objects.create(
            title="AGM notice", body="The AGM holds on Saturday.",
            channels=["in_app"],
        )
        Notification.objects.create(
            membership=member, kind=Notification.Kind.ANNOUNCEMENT,
            title="AGM notice", announcement=announcement,
        )

        meeting = Meeting.objects.create(title="2026 AGM", scheduled_at=now)
        Attendance.objects.create(meeting=meeting, membership=member)
        resolution = Resolution.objects.create(
            meeting=meeting, title="Adopt the 2025 accounts",
        )
        Vote.objects.create(resolution=resolution, membership=member,
                            choice=Vote.Choice.FOR)

        product = SavingsProduct.objects.create(name="Target Savings")
        SavingsGoal.objects.create(
            membership=member, product=product, name="School fees",
            target_amount=Decimal("100000.00"),
        )

        declaration = DividendDeclaration.objects.create(
            period_label="FY2025", total_amount=Decimal("50000.00"),
        )
        DividendAllocation.objects.create(
            declaration=declaration, membership=member,
            share_capital=member.share_capital, amount=Decimal("50000.00"),
        )

        loan_product = LoanProduct.objects.create(
            name="Emergency Loan", interest_rate=Decimal("10.00"),
            max_amount=Decimal("500000.00"),
        )
        loan = Loan.objects.create(
            membership=member, product=loan_product,
            principal=Decimal("300000.00"), interest_rate=Decimal("10.00"),
            term_months=12, status=Loan.Status.DISBURSED, disbursed_at=now,
        )
        RepaymentInstalment.objects.create(
            loan=loan, sequence=1, due_date=timezone.localdate(),
            amount_due=Decimal("27500.00"),
            principal_component=Decimal("25000.00"),
            interest_component=Decimal("2500.00"),
        )
        LoanRepayment.objects.create(loan=loan, amount=Decimal("27500.00"))

        ApprovalRequest.objects.create(
            action=ApprovalRequest.Action.LOAN_APPROVE, object_id=loan.id,
            summary=f"Loan #{loan.id}", requested_by=member.user,
        )
        ProviderAccount.objects.create(
            provider=Provider.PAYSTACK, subaccount_code="ACCT_test123",
            bank_name="GTBank",
        )
        JoinRequest.objects.create(
            full_name="Tunde Bello", email="tunde@example.com",
            phone="08031111111", message="I was introduced by my cousin.",
        )

    PaymentEvent.all_objects.create(
        cooperative=coop, provider=Provider.PAYSTACK, event_id="evt_1",
        reference="CTB-REF-1", amount=Decimal("5000.00"),
        status=PaymentEvent.Status.UNMATCHED, payload={"event": "charge.success"},
    )

    AuditLog.all_objects.create(
        cooperative=coop, actor=member.user, action="membership.created",
        entity_type="Membership", entity_id=str(member.pk),
        entity_repr=str(member),
    )

    # Platform-level rows.
    plan = Plan.objects.create(name="Small", price_monthly=Decimal("15000.00"))
    subscription = Subscription.objects.create(cooperative=coop, plan=plan)
    Invoice.objects.create(cooperative=coop, subscription=subscription,
                           number="INV-0001", amount=Decimal("15000.00"))
    OnboardingItem.objects.create(cooperative=coop)
    Domain.objects.create(cooperative=coop, domain="imole.example.africa")
    provider_status = ProviderStatus.objects.create(name="Paystack",
                                                    kind=ProviderStatus.Kind.PSP)
    ProviderCheck.objects.create(provider=provider_status,
                                 status=ProviderStatus.Status.OPERATIONAL)
    Incident.objects.create(title="Elevated webhook latency")
    SupportTicket.objects.create(cooperative=coop, subject="Cannot log in")
    PlatformProfile.load()
    NotificationTemplate.objects.create(key="welcome", name="Welcome email")
    PlatformTeamMember.objects.create(
        user=User.objects.create_user(email="lead@startupripple.co",
                                      full_name="Onboarding Lead"),
    )

    # Admin requests carry no tenant, so make the tests look like one.
    set_current_cooperative(None)
    return {"coop": coop, "member": member, "contribution": contribution,
            "loan": loan, "journal": contribution.journal}


# ── Coverage ────────────────────────────────────────────────────────────────
def test_every_project_model_is_registered(db):
    """A new model must not be quietly left out of the admin."""
    missing = [m._meta.label for m in project_models()
               if m not in admin.site._registry]
    assert missing == [], f"models missing from the admin: {missing}"


# ── The tenant-manager trap ─────────────────────────────────────────────────
def test_tenant_scoped_admins_read_unscoped(dataset, admin_request):
    """With no tenant bound, the scoping manager is empty but the admin is not.

    This is the whole reason ``core.admin.UnscopedAdminMixin`` exists: a model
    registered with a plain ``ModelAdmin`` renders an empty changelist here.
    """
    set_current_cooperative(None)
    checked = 0
    for model, model_admin in admin.site._registry.items():
        if model._meta.app_label not in PROJECT_APPS:
            continue
        if not hasattr(model, "all_objects"):
            continue
        total = model.all_objects.count()
        if not total:
            continue
        checked += 1
        assert model._default_manager.count() == 0, (
            f"{model._meta.label}: expected the scoping manager to be empty"
        )
        assert model_admin.get_queryset(admin_request).count() == total, (
            f"{model._meta.label}: admin queryset is tenant-scoped — it must "
            f"read through all_objects"
        )
    assert checked > 10


# ── Rendering ───────────────────────────────────────────────────────────────
def test_every_admin_page_renders(dataset, admin_client_2fa, admin_request):
    """Index, changelist, add and change pages for every registered model."""
    failures = []

    response = admin_client_2fa.get(reverse("admin:index"))
    if response.status_code != 200:
        failures.append(f"admin:index → {response.status_code}")

    for model, model_admin in sorted(admin.site._registry.items(),
                                     key=lambda kv: kv[0]._meta.label):
        opts = model._meta
        if opts.app_label not in PROJECT_APPS:
            continue
        base = f"admin:{opts.app_label}_{opts.model_name}"

        response = admin_client_2fa.get(reverse(base + "_changelist"))
        if response.status_code != 200:
            failures.append(f"{opts.label} changelist → {response.status_code}")

        expected = 200 if model_admin.has_add_permission(admin_request) else 403
        response = admin_client_2fa.get(reverse(base + "_add"))
        if response.status_code != expected:
            failures.append(
                f"{opts.label} add → {response.status_code} (want {expected})")

        obj = model_admin.get_queryset(admin_request).first()
        assert obj is not None, f"{opts.label} has no row to render"
        response = admin_client_2fa.get(reverse(base + "_change", args=[obj.pk]))
        if response.status_code != 200:
            failures.append(f"{opts.label} change → {response.status_code}")

    assert failures == [], "admin pages did not render: " + "; ".join(failures)


# ── Append-only records ─────────────────────────────────────────────────────
@pytest.mark.parametrize("model", [Journal, LedgerEntry, Vote])
def test_append_only_admins_are_read_only(dataset, admin_request, model):
    model_admin = admin.site._registry[model]
    assert model_admin.has_add_permission(admin_request) is False
    assert model_admin.has_change_permission(admin_request) is False
    assert model_admin.has_delete_permission(admin_request) is False
    # Every concrete field is readonly, so the detail page renders as a viewer.
    readonly = model_admin.get_readonly_fields(admin_request)
    assert {f.name for f in model._meta.fields} <= set(readonly)


def test_append_only_add_is_forbidden(dataset, admin_client_2fa):
    response = admin_client_2fa.get(reverse("admin:ledger_journal_add"))
    assert response.status_code == 403


# ── Access control ──────────────────────────────────────────────────────────
def test_admin_rejects_staff_who_are_not_platform_admins(db, client):
    """is_staff alone must not open a cross-tenant admin."""
    staffer = User.objects.create_user(
        email="clerk@imole.coop", password="pw-test-12345",
        full_name="Ordinary Clerk", is_staff=True,
    )
    assert staffer.is_platform_admin is False
    client.force_login(staffer)
    response = client.get(reverse("admin:index"))
    assert response.status_code == 302        # bounced to the admin login


def test_admin_accepts_platform_admin(admin_client_2fa):
    assert admin_client_2fa.get(reverse("admin:index")).status_code == 200


# ── 2FA on money-touching models ────────────────────────────────────────────
@pytest.mark.parametrize("model", [Contribution, Loan, Account, Membership,
                                   DividendDeclaration, PaymentEvent])
def test_financial_admins_require_two_factor(db, model):
    """Writing to a money-touching model requires 2FA — superusers included."""
    model_admin = admin.site._registry[model]

    without = RequestFactory().get("/admin/")
    without.user = User.objects.create_superuser(
        email="no2fa@startupripple.co", password="pw-test-12345",
        full_name="No 2FA", two_factor_enabled=False,
    )
    assert model_admin.has_change_permission(without) is False
    assert model_admin.has_delete_permission(without) is False

    with_2fa = RequestFactory().get("/admin/")
    with_2fa.user = User.objects.create_superuser(
        email="has2fa@startupripple.co", password="pw-test-12345",
        full_name="Has 2FA", two_factor_enabled=True,
    )
    assert model_admin.has_change_permission(with_2fa) is True


def test_user_admin_does_not_require_two_factor(db):
    """Otherwise a superuser without 2FA could never switch their own on."""
    model_admin = admin.site._registry[User]
    request = RequestFactory().get("/admin/")
    request.user = User.objects.create_superuser(
        email="bootstrap@startupripple.co", password="pw-test-12345",
        full_name="Bootstrap", two_factor_enabled=False,
    )
    assert model_admin.has_change_permission(request) is True


# ── Actions ─────────────────────────────────────────────────────────────────
def test_reverse_journal_action_posts_a_mirror(dataset, admin_request):
    journal = dataset["journal"]
    model_admin = admin.site._registry[Journal]
    before = Journal.all_objects.count()

    model_admin.reverse_selected(
        admin_request, Journal.all_objects.filter(pk=journal.pk))

    assert Journal.all_objects.count() == before + 1
    reversal = Journal.all_objects.get(reversal_of=journal)
    assert reversal.reference == f"REV-{journal.reference}"


def test_void_contribution_action_reverses_it(dataset, admin_request):
    contribution = dataset["contribution"]
    model_admin = admin.site._registry[Contribution]

    model_admin.void_selected(
        admin_request, Contribution.all_objects.filter(pk=contribution.pk))

    contribution.refresh_from_db()
    assert contribution.status == Contribution.Status.REVERSED


def test_money_actions_are_hidden_without_two_factor(db):
    request = RequestFactory().get("/admin/")
    request.user = User.objects.create_superuser(
        email="noaction@startupripple.co", password="pw-test-12345",
        full_name="No 2FA", two_factor_enabled=False,
    )
    assert "reverse_selected" not in admin.site._registry[Journal].get_actions(request)


# ── Derived columns must not be N+1 ─────────────────────────────────────────
def test_derived_columns_do_not_scale_with_row_count(
    dataset, admin_client_2fa, django_assert_max_num_queries,
):
    """Account.balance / Membership.savings_balance are aggregated, not looped.

    Without the annotations these changelists cost one extra query per row, so
    the bound below fails loudly if someone drops them.
    """
    coop = dataset["coop"]
    with use_tenant(coop):
        for n in range(40):
            Account.objects.create(code=f"90{n:02d}", name=f"Account {n}",
                                   kind=Account.Kind.EXPENSE)
    set_current_cooperative(None)
    assert Account.all_objects.count() >= 45

    with django_assert_max_num_queries(25):
        response = admin_client_2fa.get(reverse("admin:ledger_account_changelist"))
    assert response.status_code == 200

    with django_assert_max_num_queries(25):
        response = admin_client_2fa.get(
            reverse("admin:accounts_membership_changelist"))
    assert response.status_code == 200


# ── Theme ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url_name, kwargs", [
    ("admin:index", {}),
    ("admin:accounts_membership_changelist", {}),
    ("admin:ledger_account_changelist", {}),
])
def test_theme_survives_templates_that_override_extrastyle(
    dataset, admin_client_2fa, url_name, kwargs,
):
    """The stylesheet is injected via {% block extrastyle %} in base_site.html.

    Admin templates such as change_list.html override that block, so the theme
    only survives because they call {{ block.super }}. If an override ever drops
    it, the page silently reverts to stock Django blue — this catches that.
    """
    response = admin_client_2fa.get(reverse(url_name, kwargs=kwargs))
    assert response.status_code == 200
    html = response.content.decode()
    assert "cooperativeos/admin.css" in html
    assert "Bricolage+Grotesque" in html


def test_admin_header_uses_the_uploaded_platform_logo(dataset, admin_client_2fa,
                                                      tmp_path, settings):
    """An uploaded logo must reach the admin header, not just Settings.

    The template cannot read PlatformProfile on its own — the branding arrives
    through CooperativeOSAdminSite.each_context.
    """
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    from platform_admin.models import PlatformProfile

    settings.MEDIA_ROOT = tmp_path / "media"
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (11, 79, 58)).save(buffer, format="PNG")

    profile = PlatformProfile.load()
    profile.name = "Startup Ripple"
    profile.logo = SimpleUploadedFile("mark.png", buffer.getvalue(),
                                      content_type="image/png")
    profile.save()

    html = admin_client_2fa.get(reverse("admin:index")).content.decode()
    assert "platform_brand/" in html, "the uploaded logo should head the admin"
    assert "Startup Ripple" in html


def test_admin_header_falls_back_when_no_logo_is_uploaded(dataset,
                                                          admin_client_2fa):
    """Most installs have no logo yet; the built-in mark must still render."""
    html = admin_client_2fa.get(reverse("admin:index")).content.decode()
    assert "coop-logo" in html
    assert "<svg" in html, "the built-in mark is the fallback"


def test_admin_theme_handles_the_default_auto_colour_scheme():
    """Dark handling must live in the token blocks, not per-rule overrides.

    Django defaults to <html data-theme="auto">, which matches neither
    [data-theme="light"] nor [data-theme="dark"]. With a dark OS the palette
    flips through the media query, but any rule keyed on
    html[data-theme="dark"] never fires — so hardcoding a light colour in a
    chrome rule renders dark text on a dark background. That is exactly the bug
    this guards against.
    """
    import re

    css = (Path(settings.BASE_DIR) / "core" / "static" / "cooperativeos"
           / "admin.css").read_text(encoding="utf-8")
    # The file's own comments discuss this very selector, so strip them before
    # matching or the guard flags its own documentation.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    # The dark token block must not lose to an explicit light choice.
    assert ':root:not([data-theme="light"])' in css, (
        "the media-query dark block should exclude an explicit light theme"
    )

    # html[data-theme="dark"] may define tokens — that block is written
    # `html[data-theme="dark"] {`, with nothing between selector and brace.
    # What it must never do is key a *chrome* rule, i.e. carry a descendant
    # selector, because those are invisible under data-theme="auto".
    descendant_overrides = re.findall(
        r'html\[data-theme="dark"\][ \t]+[^{\s][^{]*\{', css)
    assert descendant_overrides == [], (
        "these rules never apply under data-theme=auto; use a semantic token "
        f"redefined in every block instead: {descendant_overrides[:5]}"
    )


def test_branding_renders_the_cooperativeos_wordmark(dataset, admin_client_2fa):
    html = admin_client_2fa.get(reverse("admin:index")).content.decode()
    assert "coop-logo" in html
    assert "coop-name" in html
    assert "CooperativeOS" in html


def test_login_page_is_themed(db, client):
    """login.html also overrides extrastyle, and is seen before sign-in."""
    html = client.get(reverse("admin:login")).content.decode()
    assert "cooperativeos/admin.css" in html
    assert "coop-logo" in html
