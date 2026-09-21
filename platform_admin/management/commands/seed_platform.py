"""
Seed platform-ops demo data (Startup Ripple surface): subscription plans,
a handful of extra cooperatives across states/tiers, their subscriptions and
invoices, an onboarding pipeline, white-label domains, provider health and the
platform team.

Idempotent — safe to run repeatedly and safe to run after ``seed_demo``:
    python manage.py seed_platform
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from platform_admin.models import (
    Domain, Incident, Invoice, NotificationTemplate, OnboardingItem, Plan,
    PlatformProfile, PlatformTeamMember, ProviderCheck, ProviderStatus,
    Subscription, SupportTicket,
)
from tenants.models import Cooperative
from tenants.services import provision_cooperative


def _get_or_provision(*, slug, name, **fields) -> Cooperative:
    existing = Cooperative.objects.filter(slug=slug).first()
    if existing:
        return existing
    return provision_cooperative(name=name, slug=slug, **fields)


def seed_platform_data(stdout=None, style=None):
    """Populate the platform-ops tables. Returns a short summary string."""
    today = timezone.now().date()

    # ── Plans ───────────────────────────────────────────────────────────────
    plan_specs = [
        ("Starter", Plan.Tier.SMALL, "10000", 50, 500,
         "For thrift & credit and small multipurpose coops."),
        ("Growth", Plan.Tier.MEDIUM, "35000", 500, 5000,
         "For established cooperatives scaling operations."),
        ("Institutional", Plan.Tier.LARGE, "150000", 5000, 0,
         "For large societies and federations."),
    ]
    plans = {}
    for name, tier, price, lo, hi, desc in plan_specs:
        plan, _ = Plan.objects.get_or_create(
            name=name,
            defaults=dict(tier=tier, price_monthly=Decimal(price),
                          min_members=lo, max_members=hi, description=desc),
        )
        plans[tier] = plan

    # ── Extra cooperatives (breadth for cross-tenant analytics) ─────────────
    coop_specs = [
        ("imole", "Ìmọ̀lè Multipurpose CS", "Lagos", Cooperative.Tier.MEDIUM),
        ("unity-farmers", "Unity Farmers Co-op", "Rivers",
         Cooperative.Tier.SMALL),
        ("aba-traders", "Aba Traders Cooperative", "Abia",
         Cooperative.Tier.SMALL),
        ("ahmadu-bello", "Ahmadu Bello Staff MCS", "Kaduna",
         Cooperative.Tier.MEDIUM),
        ("plateau-teachers", "Plateau Teachers Co-op", "Plateau",
         Cooperative.Tier.MEDIUM),
        ("coastal-fishers", "Coastal Fishers Society", "Rivers",
         Cooperative.Tier.SMALL),
    ]
    coops = {}
    for slug, name, state, tier in coop_specs:
        coops[slug] = _get_or_provision(
            slug=slug, name=name, state=state, tier=tier,
            coop_type="multipurpose",
        )

    # ── Subscriptions ───────────────────────────────────────────────────────
    sub_status = {
        "imole": Subscription.Status.ACTIVE,
        "unity-farmers": Subscription.Status.ACTIVE,
        "aba-traders": Subscription.Status.PAST_DUE,
        "ahmadu-bello": Subscription.Status.ACTIVE,
        "plateau-teachers": Subscription.Status.ACTIVE,
        "coastal-fishers": Subscription.Status.TRIAL,
    }
    for slug, coop in coops.items():
        Subscription.objects.get_or_create(
            cooperative=coop,
            defaults=dict(
                plan=plans[coop.tier],
                status=sub_status.get(slug, Subscription.Status.ACTIVE),
                started_at=today - timedelta(days=120),
                current_period_start=today.replace(day=1),
                current_period_end=today.replace(day=1) + timedelta(days=30),
            ),
        )

    # ── Invoices ────────────────────────────────────────────────────────────
    period = today.strftime("%b %Y")
    invoice_specs = [
        ("unity-farmers", "10000", Invoice.Status.PAID, 0),
        ("ahmadu-bello", "35000", Invoice.Status.PAID, 0),
        ("plateau-teachers", "35000", Invoice.Status.SENT, 5),
        ("aba-traders", "10000", Invoice.Status.OVERDUE, -9),
        ("imole", "35000", Invoice.Status.PAID, 0),
    ]
    year_seq = today.year
    for i, (slug, amount, status, due_offset) in enumerate(invoice_specs, 1):
        coop = coops[slug]
        number = f"INV-{year_seq}-{200 + i}"
        sub = getattr(coop, "subscription", None)
        Invoice.objects.get_or_create(
            number=number,
            defaults=dict(
                cooperative=coop, subscription=sub,
                period_label=period, amount=Decimal(amount), status=status,
                issued_at=today - timedelta(days=15),
                due_at=today + timedelta(days=due_offset),
                paid_at=today - timedelta(days=2)
                if status == Invoice.Status.PAID else None,
            ),
        )

    # ── Onboarding pipeline (provision-first: real PROSPECTIVE tenants) ──────
    from platform_admin.services import start_onboarding

    onboarding_specs = [
        ("ikeja-artisans", "Ikeja Artisans MPCS", "Lagos",
         OnboardingItem.Stage.DISCOVERY),
        ("kano-millers", "Kano Millers Union", "Kano",
         OnboardingItem.Stage.DATA_MIGRATION),
        ("enugu-civil", "Enugu Civil Servants CS", "Enugu",
         OnboardingItem.Stage.PARALLEL_RUN),
    ]
    for slug, name, state, stage in onboarding_specs:
        prospect = Cooperative.objects.filter(slug=slug).first()
        if prospect is None:
            prospect = provision_cooperative(
                name=name, slug=slug, state=state, coop_type="Multipurpose",
                status=Cooperative.Status.PROSPECTIVE)
        item = start_onboarding(prospect)
        if item.stage != stage:
            item.stage = stage
            item.target_go_live = today + timedelta(days=30)
            item.save(update_fields=["stage", "target_go_live", "updated_at"])

    # ── White-label domains ─────────────────────────────────────────────────
    Domain.objects.get_or_create(
        domain="unitysavings.ng",
        defaults=dict(cooperative=coops["unity-farmers"], is_primary=True,
                      dns_status=Domain.DNSStatus.VERIFIED,
                      ssl_status=Domain.SSLStatus.ISSUED,
                      verified_at=timezone.now()),
    )
    Domain.objects.get_or_create(
        domain="my.imole.coop",
        defaults=dict(cooperative=coops["imole"], is_primary=True,
                      dns_status=Domain.DNSStatus.PENDING,
                      ssl_status=Domain.SSLStatus.AWAITING),
    )

    # ── Provider / system health ────────────────────────────────────────────
    provider_specs = [
        ("Paystack", ProviderStatus.Kind.PSP,
         ProviderStatus.Status.OPERATIONAL, 142),
        ("Flutterwave", ProviderStatus.Kind.PSP,
         ProviderStatus.Status.OPERATIONAL, 188),
        ("Termii (SMS / USSD)", ProviderStatus.Kind.SMS,
         ProviderStatus.Status.DEGRADED, 410),
        ("Brevo (Email)", ProviderStatus.Kind.EMAIL,
         ProviderStatus.Status.OPERATIONAL, 96),
    ]
    import random
    for name, kind, status, latency in provider_specs:
        provider, _ = ProviderStatus.objects.get_or_create(
            name=name,
            defaults=dict(kind=kind, status=status, latency_ms=latency,
                          checked_at=timezone.now()),
        )
        # Seed ~20 recent latency samples so sparklines have history.
        if provider.checks.count() == 0:
            now = timezone.now()
            for i in range(20):
                jitter = random.randint(-30, 40)
                ProviderCheck.objects.create(
                    provider=provider, status=status,
                    latency_ms=max(latency + jitter, 20),
                    checked_at=now - timedelta(hours=(20 - i) * 6))

    # ── Incident log ────────────────────────────────────────────────────────
    Incident.objects.get_or_create(
        title="Termii SMS latency elevated",
        defaults=dict(severity=Incident.Severity.MINOR,
                      status=Incident.Status.MONITORING,
                      note="SMS delivery slower than usual; monitoring provider."),
    )

    # ── Platform team ───────────────────────────────────────────────────────
    owner = User.objects.filter(email="admin@startupripple.co").first()
    if owner:
        PlatformTeamMember.objects.get_or_create(
            user=owner,
            defaults=dict(role=PlatformTeamMember.Role.OWNER),
        )
    team_specs = [
        ("hauwa@startupripple.co", "Hauwa Bello",
         PlatformTeamMember.Role.ONBOARDING_LEAD),
        ("yusuf@startupripple.co", "Yusuf Ibrahim",
         PlatformTeamMember.Role.SUPPORT),
    ]
    for email, name, role in team_specs:
        user, _ = User.objects.get_or_create(
            email=email,
            defaults=dict(full_name=name, is_platform_admin=True,
                          is_staff=True),
        )
        PlatformTeamMember.objects.get_or_create(
            user=user, defaults=dict(role=role))

    # ── Support tickets ─────────────────────────────────────────────────────
    owner = User.objects.filter(email="admin@startupripple.co").first()
    ticket_specs = [
        ("aba-traders", "Invoice dispute — June", SupportTicket.Priority.HIGH,
         "Member queried the overdue invoice amount; please review."),
        ("imole", "Custom domain not resolving",
         SupportTicket.Priority.NORMAL,
         "my.imole.coop CNAME added but still shows pending."),
    ]
    for slug, subject, priority, body in ticket_specs:
        SupportTicket.objects.get_or_create(
            cooperative=coops[slug], subject=subject,
            defaults=dict(priority=priority, body=body, created_by=owner),
        )

    # ── Platform profile + notification templates ───────────────────────────
    PlatformProfile.load()
    template_specs = [
        ("welcome-onboarding", "Onboarding welcome",
         NotificationTemplate.Channel.EMAIL,
         "Welcome to {{platform_name}}, {{coop_name}}",
         "Hi {{coop_name}} team,\n\nYour cooperative is being set up on "
         "CooperativeOS. We'll guide you from data migration to go-live."),
        ("invoice-issued", "Invoice issued",
         NotificationTemplate.Channel.EMAIL,
         "Invoice for {{coop_name}} — {{amount}}",
         "An invoice of {{amount}} is due on {{date}}. Thank you."),
        ("go-live", "Go-live announcement",
         NotificationTemplate.Channel.SMS, "",
         "{{coop_name}} is now live on CooperativeOS! Members can start "
         "contributing today."),
    ]
    for key, name, channel, subject, body in template_specs:
        NotificationTemplate.objects.get_or_create(
            key=key,
            defaults=dict(name=name, channel=channel, subject=subject,
                          body=body))

    return (
        f"plans={Plan.objects.count()} "
        f"coops={Cooperative.objects.count()} "
        f"subscriptions={Subscription.objects.count()} "
        f"invoices={Invoice.objects.count()} "
        f"onboarding={OnboardingItem.objects.count()} "
        f"domains={Domain.objects.count()} "
        f"providers={ProviderStatus.objects.count()} "
        f"team={PlatformTeamMember.objects.count()} "
        f"tickets={SupportTicket.objects.count()}"
    )


class Command(BaseCommand):
    help = "Seed Startup Ripple platform-ops demo data (plans, billing, etc.)."

    def handle(self, *args, **options):
        summary = seed_platform_data()
        self.stdout.write(self.style.SUCCESS(
            f"Seeded platform-ops demo data: {summary}"
        ))
