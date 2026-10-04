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

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


def make_pay_token() -> str:
    """An unguessable handle for an invoice's public "pay now" link.

    A named module-level function, not a lambda: Django serialises the
    reference into the migration, and a lambda cannot be serialised.
    """
    return secrets.token_urlsafe(32)


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

    # Paystack reference for this invoice, so a returning payer can be settled
    # against it. Distinct from a member contribution's reference: this is the
    # platform charging a cooperative, not a cooperative collecting from a
    # member, and it settles to the platform's own account.
    psp_reference = models.CharField(max_length=64, blank=True, db_index=True)
    # Unguessable handle for the "pay now" link in the invoice email. An officer
    # follows it from their inbox with no session, so the token *is* the
    # authorisation — hence secrets, not a sequential id.
    #
    # Indexed but deliberately not unique=True: adding a unique column with a
    # default to a table that already holds invoices makes Django write the
    # same computed value to every existing row, which violates the constraint
    # on the spot. Tokens are issued at creation instead, and 32 bytes from
    # secrets makes a collision not worth engineering against. Blank means
    # "no pay link" — the state every pre-existing invoice is in.
    pay_token = models.CharField(max_length=64, blank=True, default="",
                                 db_index=True, editable=False)
    # Reminder bookkeeping, so the cycle can run daily and still only mail every
    # BILLING_REMINDER_INTERVAL_DAYS, and so a stuck invoice is visible.
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    reminder_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-issued_at", "-created_at"]

    def __str__(self) -> str:
        return f"{self.number} · {self.cooperative}"

    @property
    def is_settled(self) -> bool:
        return self.status in (self.Status.PAID, self.Status.VOID)


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
    # Recorded only when the relay accepted the go-live email. Null therefore
    # means "never sent", which is a different problem from "sent but never
    # arrived" — and telling them apart is the whole point of storing it.
    golive_email_sent_at = models.DateTimeField(
        null=True, blank=True, editable=False,
        help_text="When the go-live announcement was last sent to the "
                  "cooperative. Empty means it has never gone out.",
    )

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

                # Start the free trial the moment they go live. Returns None
                # when no plan matches the tier, which is deliberately not
                # fatal: a missing price list is our problem, and refusing to
                # activate a cooperative over it would put it in front of
                # their members.
                from platform_admin.billing import ensure_subscription

                ensure_subscription(coop)

                # Tell the cooperative it is live. Dispatched after commit so
                # a mail failure can never roll back an activation, and sent
                # from inside this branch so advancing Go-live -> Done does
                # not announce it a second time.
                from django.db import transaction

                from platform_admin.services import notify_go_live

                transaction.on_commit(lambda: notify_go_live(self))
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
    logo = models.ImageField(
        upload_to="platform_brand/", null=True, blank=True,
        help_text="The platform's own mark, used where no cooperative brand "
                  "applies (sign-in, platform console, default emails).",
    )
    favicon = models.ImageField(
        upload_to="platform_brand/", null=True, blank=True,
        help_text="Square mark for the browser tab.",
    )
    # Shown in the public site's footer and behind every "Talk to us" link, so
    # it has to be an address that actually receives mail.
    support_email = models.EmailField(default="info@mycooperativeos.com")
    support_phone = models.CharField(max_length=20, blank=True)
    default_currency = models.CharField(max_length=3,
                                        default=settings.DEFAULT_CURRENCY)
    default_timezone = models.CharField(max_length=40, default="Africa/Lagos")
    # Editable by any platform admin under Platform Settings → Branding, and
    # served through /public/branding/ so the public site can pick it up without
    # a rebuild. Blank is meaningful: no tracking script is loaded at all.
    ga_measurement_id = models.CharField(
        max_length=32, blank=True,
        verbose_name="Google Analytics measurement ID",
        help_text='GA4 measurement ID, e.g. "G-XXXXXXXXXX". Leave blank to '
                  'switch tracking off entirely.',
    )

    def __str__(self) -> str:
        return self.name

    def clean(self):
        """Normalise a pasted Google tag down to its measurement ID.

        Covers the Django admin, whose ModelForm runs full_clean. The DRF
        serializer validates the same way via the same helper.
        """
        super().clean()
        from django.core.exceptions import ValidationError

        from platform_admin.analytics import extract_measurement_id

        try:
            self.ga_measurement_id = extract_measurement_id(
                self.ga_measurement_id)
        except ValueError as exc:
            raise ValidationError({"ga_measurement_id": str(exc)}) from exc

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


# ── Public site content (the CMS) ───────────────────────────────────────────
# Everything below exists so the marketing site can be edited by someone who
# does not write code. The rule that shapes it: no copy that an admin might
# reasonably want to change may live in the React bundle, because changing it
# there means a developer, a build and a deploy.
#
# Structure is fixed (a hero, a principle band, features, steps, pricing,
# footer); wording and imagery are data. Adding a *section* is still a code
# change — that is a deliberate trade for an admin screen that reads like a
# form rather than a page builder.

# Material Symbols ligatures, offered as a dropdown. A free-text field here
# would ask a non-technical editor to guess an icon name and show them a blank
# space when they guessed wrong.
ICON_CHOICES = [
    ("group", "People"),
    ("savings", "Savings"),
    ("request_quote", "Loan / quote"),
    ("how_to_vote", "Voting"),
    ("summarize", "Reports"),
    ("campaign", "Announcements"),
    ("account_balance", "Bank"),
    ("receipt_long", "Receipts"),
    ("verified_user", "Security"),
    ("lock", "Lock"),
    ("insights", "Insights"),
    ("handshake", "Partnership"),
    ("payments", "Payments"),
    ("schedule", "Time"),
    ("support_agent", "Support"),
    ("cloud_done", "Cloud"),
    ("fact_check", "Audit"),
    ("diversity_3", "Community"),
]


class SiteContent(TimeStampedModel):
    """Singleton: the wording and imagery of the public landing page.

    Field defaults hold the live copy rather than placeholders, and a data
    migration creates the row from them. That matters for usability: an
    editor opening this screen sees exactly what the site currently says and
    edits it in place, instead of facing empty boxes with no idea what the
    original wording was.
    """

    # ── Hero ────────────────────────────────────────────────────────────────
    hero_eyebrow = models.CharField(
        max_length=160,
        default="Built for cooperatives and credit unions of 50–5,000 members",
        help_text="The small pill of text above the headline.",
    )
    hero_title = models.CharField(
        max_length=200,
        default="A digital operating layer for cooperatives everywhere.",
        help_text="The main headline. Keep it to one sentence.",
    )
    hero_subtitle = models.TextField(
        default="Membership, contributions, loans, governance and audit-ready "
                "reporting — in one place, on an append-only double-entry "
                "ledger your auditor can actually follow.",
        help_text="The paragraph under the headline.",
    )
    hero_primary_cta = models.CharField(max_length=40, default="Get started")
    hero_secondary_cta = models.CharField(
        max_length=40, default="Sign in to your cooperative")
    hero_image = models.ImageField(
        upload_to="site_content/", null=True, blank=True,
        help_text="A wide photograph beside the headline. Landscape, at least "
                  "1200px wide. Leave empty to use the built-in default "
                  "photograph.",
    )
    hero_image_alt = models.CharField(
        max_length=200, blank=True,
        help_text="Describes the image for screen readers and when it fails "
                  "to load. e.g. 'Members of a savings group meeting'.",
    )

    # ── Principle band ──────────────────────────────────────────────────────
    principle_eyebrow = models.CharField(
        max_length=80, default="Our core principle")
    principle_title = models.CharField(
        max_length=120, default="Every naira, traceable.")
    # This band used to be about custody — first claiming we never hold member
    # funds (which the disbursement wallet made untrue), then explaining the
    # wallet. Neither belongs in a marketing headline: the first was false and
    # the second foregrounds an edge case that only applies to societies
    # choosing to lend electronically.
    #
    # It now says what is true of every society without making a claim either
    # way about custody. The wallet is still disclosed, but where it matters:
    # in the Fund wallet flow (the moment an officer decides to deposit), the
    # officer Guide, and the policy documents. Do not reintroduce a "we never
    # hold funds" line here — that is the sentence that became false.
    principle_body = models.TextField(
        default="Contributions settle through licensed payment providers "
                "straight into your cooperative's own bank account. Every "
                "movement is recorded in an append-only double-entry ledger — "
                "nothing is edited or deleted, corrections are posted as "
                "reversals, and your auditor can follow any figure back to the "
                "entry that produced it.",
    )

    # ── Section headings ────────────────────────────────────────────────────
    features_title = models.CharField(
        max_length=120, default="Everything a cooperative runs on")
    features_intro = models.TextField(
        default="Replacing the member register, the contribution book and the "
                "annual scramble to produce accounts.",
    )
    steps_title = models.CharField(max_length=120, default="How it works")
    gallery_title = models.CharField(
        max_length=120, default="Built for the people who run cooperatives",
        help_text="Heading above the photo strip.",
    )
    gallery_intro = models.TextField(
        blank=True,
        default="Treasurers, secretaries and members — the people who keep a "
                "cooperative running, wherever it is.",
    )

    # ── Pricing ─────────────────────────────────────────────────────────────
    pricing_title = models.CharField(
        max_length=120, default="Pricing that follows your size")
    pricing_intro = models.TextField(
        default="A monthly subscription per cooperative — not a cut of your "
                "members' money.",
    )
    pricing_fallback = models.TextField(
        default="Pricing depends on your cooperative's size and how much "
                "history "
                "needs migrating.",
        help_text="Shown when no plans are published yet. The 'talk to us' "
                  "link is added automatically.",
    )

    # ── Footer ──────────────────────────────────────────────────────────────
    footer_tagline = models.CharField(max_length=120, default="by Startup Ripple")
    footer_disclaimer = models.TextField(
        default="CooperativeOS records and reports on cooperative finances. "
                "It is not a bank, a lender, or a deposit-taking institution, "
                "and it does not hold member funds.",
        help_text="Legal footnote at the very bottom. Changing this may have "
                  "regulatory consequences — check before editing.",
    )

    # ── Search engines / link previews ──────────────────────────────────────
    meta_title = models.CharField(
        max_length=70, blank=True,
        help_text="Browser tab and search-result title. Under 60 characters "
                  "is best. Leave empty to use the platform name.",
    )
    meta_description = models.CharField(
        max_length=200, blank=True,
        default="Membership, contributions, loans and audit-ready reporting "
                "for cooperatives, on an append-only ledger.",
        help_text="The grey text under the title in search results.",
    )

    # ── Feature deep-dives ──────────────────────────────────────────────────
    showcase_title = models.CharField(
        max_length=120, default="A closer look at the day-to-day")
    showcase_intro = models.TextField(
        blank=True,
        default="The parts of running a cooperative that take the most time, "
                "and "
                "what replaces them.",
    )

    # ── How it works ────────────────────────────────────────────────────────
    steps_intro = models.TextField(
        blank=True,
        default="From your first conversation with us to members checking "
                "their own statements — usually six to ten weeks.",
    )

    # ── Mobile apps ─────────────────────────────────────────────────────────
    apps_title = models.CharField(
        max_length=120, default="Put it in your members' hands")
    apps_body = models.TextField(
        default="Members check their contributions, savings and loan balance, "
                "read statements and get notices — without calling the "
                "secretary. Officers can approve from the same app.",
    )
    apps_note = models.CharField(
        max_length=160, blank=True,
        help_text="Small line under the buttons, e.g. 'iOS version coming "
                  "soon'. Leave empty to hide it.",
    )
    android_store_url = models.URLField(
        blank=True,
        help_text="Google Play link. Leave empty if not published there yet.",
    )
    android_apk = models.FileField(
        upload_to="site_apps/", null=True, blank=True,
        help_text="Or upload the APK directly for side-loading. If a Play "
                  "Store link is set above, that is used instead. Large "
                  "uploads can hit the host's file-size limit — upload over "
                  "SFTP and link to it if the admin times out.",
    )
    ios_store_url = models.URLField(
        blank=True, help_text="App Store link. Leave empty to hide the "
                              "iOS button.")
    app_screenshot = models.ImageField(
        upload_to="site_content/", null=True, blank=True,
        help_text="A phone screenshot beside the download buttons. Portrait.",
    )
    app_screenshot_alt = models.CharField(max_length=200, blank=True)

    # ── Closing call to action ──────────────────────────────────────────────
    cta_title = models.CharField(
        max_length=160, default="Ready to bring your cooperative on board?")
    cta_body = models.TextField(
        default="Tell us about your cooperative and we will talk through what "
                "needs migrating. Nothing is charged until you decide to go "
                "ahead.",
    )
    cta_button = models.CharField(max_length=40,
                                  default="Apply as a cooperative")
    cta_image = models.ImageField(
        upload_to="site_content/", null=True, blank=True,
        help_text="Photograph behind the closing banner. A wide, fairly dark "
                  "image works best — text sits on top of it. Leave empty to "
                  "use the built-in default.",
    )
    cta_image_alt = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Site content"
        verbose_name_plural = "Site content"

    def __str__(self) -> str:
        return "Public site content"

    @classmethod
    def load(cls) -> "SiteContent":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SiteSectionItem(TimeStampedModel):
    """Shared behaviour for the ordered, hideable lists below.

    ``visible`` rather than deletion is the default way to take something off
    the site: an editor who removes a feature to try a shorter page can put it
    back without retyping it.
    """

    order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers appear first.")
    visible = models.BooleanField(
        default=True, help_text="Untick to hide from the site without "
                                "deleting it.")

    class Meta:
        abstract = True
        ordering = ["order", "id"]


class SiteFeature(SiteSectionItem):
    """One card in the "Everything a cooperative runs on" grid."""

    icon = models.CharField(max_length=40, choices=ICON_CHOICES,
                            default="group")
    title = models.CharField(max_length=80)
    body = models.TextField()

    class Meta(SiteSectionItem.Meta):
        abstract = False
        ordering = ["order", "id"]
        verbose_name = "Site feature card"

    def __str__(self) -> str:
        return self.title


class SiteStep(SiteSectionItem):
    """One numbered step in "How it works"."""

    number = models.CharField(
        max_length=4, help_text="Shown above the step, e.g. 01.")
    icon = models.CharField(
        max_length=40, choices=ICON_CHOICES, default="handshake",
        help_text="Shown in the diagram above the steps.",
    )
    title = models.CharField(max_length=80)
    body = models.TextField()

    class Meta(SiteSectionItem.Meta):
        abstract = False
        ordering = ["order", "id"]
        verbose_name = "Site how-it-works step"

    def __str__(self) -> str:
        return f"{self.number} · {self.title}"


class SiteTrustBadge(SiteSectionItem):
    """One line in the trust strip above the footer."""

    text = models.CharField(max_length=120)
    icon = models.CharField(max_length=40, choices=ICON_CHOICES,
                            default="verified_user")

    class Meta(SiteSectionItem.Meta):
        abstract = False
        ordering = ["order", "id"]
        verbose_name = "Site trust badge"

    def __str__(self) -> str:
        return self.text


class SiteGalleryImage(SiteSectionItem):
    """A photograph in the "people who run cooperatives" strip.

    The strip ships with default photographs bundled in the frontend. Adding
    any row here replaces the whole default set, so the strip is either
    entirely yours or entirely ours — never a mix, which would look accidental.

    See frontend/src/assets/site/CREDITS.md for what the defaults are and the
    licensing caveat that applies to photographs of identifiable people.
    """

    image = models.ImageField(
        upload_to="site_content/",
        help_text="Landscape, at least 800px wide. Photographs of real "
                  "people work far better here than stock illustrations.",
    )
    alt_text = models.CharField(
        max_length=200,
        help_text="Describes the photo for screen readers. Required.",
    )
    caption = models.CharField(
        max_length=120, blank=True,
        help_text="Optional line shown under the photo.",
    )

    class Meta(SiteSectionItem.Meta):
        abstract = False
        ordering = ["order", "id"]
        verbose_name = "Site gallery photo"

    def __str__(self) -> str:
        return self.caption or self.alt_text


class SiteShowcase(SiteSectionItem):
    """One alternating image-and-text block explaining a feature properly.

    The feature cards are deliberately terse — six of them have to scan at a
    glance. These are where a claim gets room to be explained, so each one
    pairs a photograph with a few sentences and some concrete bullet points.
    """

    title = models.CharField(max_length=100)
    body = models.TextField(help_text="Two or three sentences.")
    bullets = models.TextField(
        blank=True,
        help_text="One per line. Shown as a ticked list under the text. "
                  "Leave empty for none.",
    )
    image = models.ImageField(
        upload_to="site_content/", null=True, blank=True,
        help_text="Landscape, at least 1000px wide. Leave empty and the block "
                  "falls back to one of the built-in default photographs.",
    )
    image_alt = models.CharField(
        max_length=200, blank=True,
        help_text="Describes the photo for screen readers.",
    )

    class Meta(SiteSectionItem.Meta):
        abstract = False
        ordering = ["order", "id"]
        verbose_name = "Site feature deep-dive"

    def __str__(self) -> str:
        return self.title

    def bullet_list(self) -> list[str]:
        """``bullets`` split into lines, blanks dropped."""
        return [line.strip() for line in self.bullets.splitlines()
                if line.strip()]


class LegalDocument(TimeStampedModel):
    """A policy page written by someone qualified and pasted in here.

    Privacy notice, terms of service, a funds/custody disclosure — anything a
    lawyer drafts and the platform has to publish. Kept in the database rather
    than in code so updating a policy is an admin task, not a deployment.

    ``published`` defaults to **False** deliberately: a half-pasted draft must
    not appear on the public site, and a policy is usually pasted in stages.

    The site keeps working before any row exists. ``/privacy`` falls back to the
    notice built into the frontend, which matters because that URL is already
    registered with Google Play — it must never go blank waiting for legal copy.
    """

    SLUG_HELP = (
        "URL segment: the page is served at /&lt;slug&gt;. Two slugs are wired to "
        "routes and must keep their exact spelling — <b>privacy</b> and "
        "<b>terms</b>. The privacy one is registered with Google Play, so "
        "renaming it breaks the store listing."
    )
    BODY_HELP = (
        "Paste the drafted text. Formatting understood: a blank line starts a "
        "new paragraph, <code>## </code> a heading, <code>### </code> a "
        "sub-heading, <code>- </code> a bullet, <code>**bold**</code> for bold, "
        "and <code>[text](https://…)</code> for a link. Anything else appears "
        "as written — HTML is not interpreted, so tags would show as text."
    )

    slug = models.SlugField(max_length=60, unique=True, help_text=SLUG_HELP)
    title = models.CharField(
        max_length=200,
        help_text="Shown as the page heading and in the footer link.")
    body = models.TextField(help_text=BODY_HELP)
    effective_date = models.DateField(
        null=True, blank=True,
        help_text="Shown under the title as the date the policy takes effect. "
                  "Leave blank to show nothing.")
    published = models.BooleanField(
        default=False,
        help_text="Off until the text is complete. An unpublished document is "
                  "invisible to visitors and absent from the footer.")
    order = models.PositiveIntegerField(
        default=0, help_text="Order in the footer. Lower numbers first.")

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "Legal / policy document"
        verbose_name_plural = "Legal / policy documents"

    def __str__(self) -> str:
        return f"{self.title} (/{self.slug})"
