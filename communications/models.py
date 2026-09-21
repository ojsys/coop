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
