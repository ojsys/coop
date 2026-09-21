"""
Platform-level (Startup Ripple) operational models.

Unlike the cooperative-facing apps, nothing here is tenant-scoped: these tables
describe the *business of running the platform* — subscription plans, billing,
onboarding pipeline, white-label domains, third-party provider health and the
platform team. They aggregate across every cooperative and are only ever
touched by platform admins, so they inherit plain ``TimeStampedModel`` (no
``cooperative`` tenant key / auto-filtering manager). Rows that belong to a
specific cooperative carry an explicit, nullable FK to it.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


# ── Billing ────────────────────────────────────────────────────────────────
class Plan(TimeStampedModel):
    """A subscription plan a cooperative can be billed on."""

    class Tier(models.TextChoices):
        SMALL = "small", "Small (50-500)"
        MEDIUM = "medium", "Medium (500-5,000)"
        LARGE = "large", "Large / Institutional"

    name = models.CharField(max_length=120)
    tier = models.CharField(max_length=10, choices=Tier.choices,
                            default=Tier.SMALL)
    price_monthly = models.DecimalField(max_digits=12, decimal_places=2,
                                        default=0)
    currency = models.CharField(max_length=3,
                                default=settings.DEFAULT_CURRENCY)
    min_members = models.PositiveIntegerField(default=0)
    max_members = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["price_monthly", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_tier_display()})"


class Subscription(TimeStampedModel):
    """Ties a cooperative to the plan it pays for."""

    class Status(models.TextChoices):
        TRIAL = "trial", "Trial"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        CANCELED = "canceled", "Canceled"

    cooperative = models.OneToOneField(
        "tenants.Cooperative", on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT,
                             related_name="subscriptions")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.TRIAL)
    started_at = models.DateField(null=True, blank=True)
    current_period_start = models.DateField(null=True, blank=True)
    current_period_end = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.cooperative} → {self.plan.name}"


class Invoice(TimeStampedModel):
    """A billing document raised against a cooperative."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        VOID = "void", "Void"

    cooperative = models.ForeignKey(
        "tenants.Cooperative", on_delete=models.CASCADE,
        related_name="invoices",
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invoices",
    )
    number = models.CharField(max_length=40, unique=True)
    period_label = models.CharField(max_length=40, blank=True,
                                    help_text="e.g. 'Jun 2026'")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3,
                                default=settings.DEFAULT_CURRENCY)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.DRAFT)
    issued_at = models.DateField(null=True, blank=True)
    due_at = models.DateField(null=True, blank=True)
    paid_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.number} · {self.cooperative}"


# ── Onboarding pipeline ─────────────────────────────────────────────────────
class OnboardingItem(TimeStampedModel):
    """A cooperative moving through the onboarding pipeline toward go-live."""

    class Stage(models.TextChoices):
        DISCOVERY = "discovery", "Discovery"
        DATA_MIGRATION = "data_migration", "Data migration"
        PARALLEL_RUN = "parallel_run", "Parallel run"
        GO_LIVE = "go_live", "Go-live"
        DONE = "done", "Completed"

    # Ordered stages used by the `advance` action.
    STAGE_ORDER = [
        Stage.DISCOVERY, Stage.DATA_MIGRATION, Stage.PARALLEL_RUN,
        Stage.GO_LIVE, Stage.DONE,
    ]

    cooperative = models.ForeignKey(
        "tenants.Cooperative", on_delete=models.CASCADE, null=True, blank=True,
        related_name="onboarding_items",
    )
    prospect_name = models.CharField(
        max_length=200, blank=True,
        help_text="Used before a Cooperative record exists.",
    )
    stage = models.CharField(max_length=20, choices=Stage.choices,
                             default=Stage.DISCOVERY)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="owned_onboardings",
    )
    target_go_live = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["stage", "-created_at"]

    def __str__(self) -> str:
        return f"{self.display_name} @ {self.get_stage_display()}"

    @property
    def display_name(self) -> str:
        if self.cooperative_id:
            return self.cooperative.name
        return self.prospect_name or "Unnamed prospect"

    def advance(self, actor=None):
        """Move to the next pipeline stage (no-op once completed).

        Reaching Go-live (or Done) activates the linked cooperative, so the
        onboarding pipeline drives the tenant's real lifecycle.
        """
        from tenants.models import Cooperative

        order = self.STAGE_ORDER
        idx = order.index(self.stage)
        if idx >= len(order) - 1:
            return self

        self.stage = order[idx + 1]
        self.save(update_fields=["stage", "updated_at"])

        if self.stage in (self.Stage.GO_LIVE, self.Stage.DONE) \
                and self.cooperative_id:
            coop = self.cooperative
            if coop.status != Cooperative.Status.ACTIVE:
                before = coop.status
                coop.status = Cooperative.Status.ACTIVE
                coop.save(update_fields=["status", "updated_at"])
                from audit.services import record_action
                record_action(
                    cooperative=coop, actor=actor,
                    action="cooperative.go_live", entity=coop,
                    before={"status": before}, after={"status": coop.status},
                )
        return self


# ── White-label domains ─────────────────────────────────────────────────────
class Domain(TimeStampedModel):
    """A custom / white-label domain pointed at a cooperative's tenant."""

    class DNSStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"

    class SSLStatus(models.TextChoices):
        AWAITING = "awaiting", "Awaiting"
        ISSUED = "issued", "Issued"
        FAILED = "failed", "Failed"

    cooperative = models.ForeignKey(
        "tenants.Cooperative", on_delete=models.CASCADE,
        related_name="domains",
    )
    domain = models.CharField(max_length=255, unique=True)
    is_primary = models.BooleanField(default=False)
    dns_status = models.CharField(max_length=10, choices=DNSStatus.choices,
                                  default=DNSStatus.PENDING)
    ssl_status = models.CharField(max_length=10, choices=SSLStatus.choices,
                                  default=SSLStatus.AWAITING)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["domain"]

    def __str__(self) -> str:
        return self.domain


# ── Provider / system health ────────────────────────────────────────────────
class ProviderStatus(TimeStampedModel):
    """Health of an external provider the platform depends on."""

    class Kind(models.TextChoices):
        PSP = "psp", "Payments"
        SMS = "sms", "SMS / USSD"
        EMAIL = "email", "Email"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPERATIONAL = "operational", "Operational"
        DEGRADED = "degraded", "Degraded"
        DOWN = "down", "Down"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices,
                            default=Kind.OTHER)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.OPERATIONAL)
    latency_ms = models.PositiveIntegerField(default=0)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["kind", "name"]
        verbose_name_plural = "provider statuses"

    def __str__(self) -> str:
        return f"{self.name} ({self.get_status_display()})"


class ProviderCheck(TimeStampedModel):
    """An append-only latency/status sample for a provider — builds history so
    the health page can show a trend rather than a single reading."""

    provider = models.ForeignKey(
        ProviderStatus, on_delete=models.CASCADE, related_name="checks")
    status = models.CharField(max_length=12,
                              choices=ProviderStatus.Status.choices)
    latency_ms = models.PositiveIntegerField(default=0)
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["checked_at"]

    def __str__(self) -> str:
        return f"{self.provider.name} @ {self.checked_at:%Y-%m-%d %H:%M}"


class Incident(TimeStampedModel):
    """A platform incident (manual or triaged) shown on the health page."""

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        MINOR = "minor", "Minor"
        MAJOR = "major", "Major"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        MONITORING = "monitoring", "Monitoring"
        RESOLVED = "resolved", "Resolved"

    title = models.CharField(max_length=200)
    severity = models.CharField(max_length=10, choices=Severity.choices,
                                default=Severity.MINOR)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.OPEN)
    note = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"[{self.get_severity_display()}] {self.title}"


# ── Support tickets ─────────────────────────────────────────────────────────
class SupportTicket(TimeStampedModel):
    """A support request raised by the platform team against a cooperative."""

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        CLOSED = "closed", "Closed"

    cooperative = models.ForeignKey(
        "tenants.Cooperative", on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    priority = models.CharField(max_length=8, choices=Priority.choices,
                                default=Priority.NORMAL)
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="raised_tickets",
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.subject} · {self.cooperative}"


# ── Platform profile & notification templates ───────────────────────────────
class PlatformProfile(TimeStampedModel):
    """Singleton describing the platform owner (Startup Ripple) — its own brand,
    support contact and defaults applied to new cooperatives."""

    name = models.CharField(max_length=120, default="Startup Ripple")
    brand_color = models.CharField(max_length=7, default="#0b4f3a")
    support_email = models.EmailField(default="support@cooperativeos.africa")
    default_currency = models.CharField(max_length=3,
                                        default=settings.DEFAULT_CURRENCY)
    default_timezone = models.CharField(max_length=40, default="Africa/Lagos")

    def __str__(self) -> str:
        return self.name

    @classmethod
    def load(cls) -> "PlatformProfile":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class NotificationTemplate(TimeStampedModel):
    """An editable message template used for onboarding, billing and comms."""

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"
        IN_APP = "in_app", "In-app"

    key = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=8, choices=Channel.choices,
                               default=Channel.EMAIL)
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField(
        blank=True,
        help_text="Supports {{placeholders}} like {{coop_name}}, {{amount}}.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_channel_display()})"


# ── Platform team ───────────────────────────────────────────────────────────
class PlatformTeamMember(TimeStampedModel):
    """A member of the Startup Ripple platform team.

    Wraps a platform-admin ``User`` with a platform-facing role. Kept separate
    from cooperative ``Membership`` (which is tenant-scoped) because this is
    about who runs the platform, not who belongs to a coop.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ONBOARDING_LEAD = "onboarding_lead", "Onboarding lead"
        SUPPORT = "support", "Support"
        FINANCE = "finance", "Finance"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="platform_team_member",
    )
    role = models.CharField(max_length=20, choices=Role.choices,
                            default=Role.SUPPORT)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["role", "user__full_name"]

    def __str__(self) -> str:
        return f"{self.user.full_name} · {self.get_role_display()}"
