"""
Requests to join an existing cooperative.

A person who is not yet a member has no account, so a join request deliberately
stores their details rather than creating a ``User`` up front — an unapproved
stranger must not hold credentials for a society they do not belong to. The
``User`` and ``Membership`` are created only when an officer approves, at which
point the member is emailed a set-password link.

Kept in its own module rather than ``accounts/models.py`` to keep the identity
and RBAC models together and uncluttered; it is imported from there so Django
still discovers it.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class JoinRequest(TenantScopedModel, TimeStampedModel):
    """Someone asking a specific society to admit them as a member."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    # Anything the applicant wants the officers to know — an existing member
    # number from the paper register, who introduced them, and so on.
    message = models.CharField(max_length=500, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="decided_join_requests",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)

    # Set on approval, so the request links to the member it created.
    membership = models.OneToOneField(
        "accounts.Membership", on_delete=models.SET_NULL, null=True,
        blank=True, related_name="join_request",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The officer queue is "pending, for my society, newest first".
            models.Index(fields=["cooperative", "status", "-created_at"],
                         name="joinreq_queue_idx"),
        ]
        # NOTE: "one *pending* request per address per society" is enforced in
        # accounts.join_services, not by a constraint. It needs a partial unique
        # index, and Django reports supports_partial_indexes = False for MySQL
        # and MariaDB — the constraint would exist on SQLite (so tests would
        # pass) and be silently dropped in production. A plain unique constraint
        # is wrong too: a rejected applicant must be able to re-apply.

    def __str__(self) -> str:
        return f"{self.full_name} → {self.cooperative_id} ({self.status})"
