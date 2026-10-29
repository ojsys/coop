"""
Communication & engagement (PRD §6.6): announcements fanned out across in-app,
email, SMS and WhatsApp, plus transactional notifications — all respecting each
member's channel preferences and opt-outs.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class ChannelPreference(TenantScopedModel, TimeStampedModel):
    """A member's per-channel opt-in state. Absence implies all channels on."""

    membership = models.OneToOneField(
        "accounts.Membership", on_delete=models.CASCADE,
        related_name="channel_preference",
    )
    in_app = models.BooleanField(default=True)
    email = models.BooleanField(default=True)
    sms = models.BooleanField(default=True)
    whatsapp = models.BooleanField(default=True)
    # Global opt-out overrides everything except in-app.
    opted_out = models.BooleanField(default=False)

    def allows(self, channel: str) -> bool:
        if channel == "in_app":
            return self.in_app
        if self.opted_out:
            return False
        return getattr(self, channel, False)


class Announcement(TenantScopedModel, TimeStampedModel):
    class Audience(models.TextChoices):
        ALL = "all", "All members"
        ROLE = "role", "By role"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"

    title = models.CharField(max_length=200)
    body = models.TextField()
    audience = models.CharField(
        max_length=8, choices=Audience.choices, default=Audience.ALL,
    )
    audience_role = models.ForeignKey(
        "accounts.Role", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="announcements",
    )
    # Channels selected for fan-out, e.g. ["in_app", "email", "sms"].
    channels = models.JSONField(default=list)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.DRAFT,
    )
    reach = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="announcements",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class Notification(TenantScopedModel, TimeStampedModel):
    """An in-app notification (announcement fan-out or a transactional event)."""

    class Kind(models.TextChoices):
        ANNOUNCEMENT = "announcement", "Announcement"
        CONTRIBUTION = "contribution", "Contribution received"
        STATEMENT = "statement", "Statement ready"
        MEETING = "meeting", "Meeting reminder"
        VOTE = "vote", "Vote open"
        LOAN = "loan", "Loan update"
        # Its own kind rather than reusing CONTRIBUTION, which reads
        # "Contribution received" — the opposite of what a payout is.
        WITHDRAWAL = "withdrawal", "Savings withdrawal"
        DIVIDEND = "dividend", "Dividend"
        VERIFICATION = "verification", "Profile verification"

    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=14, choices=Kind.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    announcement = models.ForeignKey(
        Announcement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notifications",
    )
    read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.title}"


class MemberEmail(TenantScopedModel, TimeStampedModel):
    """An email from a society's officers to its members.

    Separate from :class:`Announcement`, which is a short notice fanned out
    across channels in one go. This is email only, written as a draft and sent
    once, in slices (``communications.member_mailing``), with a row per
    recipient so what each member was sent — and whether it went — stays
    answerable. It also reaches audiences an announcement cannot: borrowers,
    members behind on a repayment, or members picked by hand.
    """

    class Purpose(models.TextChoices):
        ANNOUNCEMENT = "announcement", "Announcement"
        REMINDER = "reminder", "Reminder"
        RECOVERY = "recovery", "Loan recovery"
        GENERAL = "general", "General"

    class Audience(models.TextChoices):
        ALL = "all", "All active members"
        ROLE = "role", "Members holding a role"
        BORROWERS = "borrowers", "Members repaying a loan"
        OVERDUE = "overdue", "Members with an overdue repayment"
        SELECTED = "selected", "Selected members"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"

    purpose = models.CharField(max_length=12, choices=Purpose.choices,
                               default=Purpose.ANNOUNCEMENT)
    subject = models.CharField(max_length=200)
    body = models.TextField(
        help_text="Plain text with the same marks as platform email. "
                  "Placeholders such as {{name}} and {{loan_balance}} are "
                  "filled in for each member.")
    audience = models.CharField(max_length=10, choices=Audience.choices,
                                default=Audience.ALL)
    audience_role = models.ForeignKey(
        "accounts.Role", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")
    members = models.ManyToManyField(
        "accounts.Membership", blank=True, related_name="+",
        help_text="For the selected audience: who to write to.")
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="member_emails_written")
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="member_emails_sent")
    sent_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.subject


class MemberEmailRecipient(TenantScopedModel, TimeStampedModel):
    """One member a :class:`MemberEmail` went (or is going) to."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    email = models.ForeignKey(MemberEmail, on_delete=models.CASCADE,
                              related_name="recipients")
    membership = models.ForeignKey(
        "accounts.Membership", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")
    address = models.EmailField()
    name = models.CharField(max_length=200, blank=True)
    member_no = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["email", "address"],
                                    name="one_row_per_address_per_member_email"),
        ]

    def __str__(self) -> str:
        return f"{self.address} · {self.email}"
