"""A person's request to have their account deleted.

Per *person*, not per membership: one account can belong to several
societies, and deleting it closes all of them. Not tenant-scoped for the same
reason — each society's officers see the requests from their own members.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TimeStampedModel


class AccountDeletionRequest(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Source(models.TextChoices):
        APP = "app", "In the app"
        WEB = "web", "Public web form"

    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE,
                             related_name="deletion_requests")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    source = models.CharField(max_length=5, choices=Source.choices,
                              default=Source.APP)
    reason = models.CharField(max_length=500, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One open request per person; a second ask returns the first.
            models.UniqueConstraint(
                fields=["user"], condition=models.Q(status="pending"),
                name="one_pending_deletion_per_user"),
        ]

    def __str__(self) -> str:
        return f"Deletion #{self.pk} ({self.status})"
