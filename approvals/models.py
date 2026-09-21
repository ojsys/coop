"""
Maker-checker approvals. One officer (the *maker*) submits a sensitive action;
a different officer (the *checker*) approves it, at which point the action is
executed. Enforces segregation of duties (checker != maker).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel, TimeStampedModel


class ApprovalRequest(TenantScopedModel, TimeStampedModel):
    class Action(models.TextChoices):
        LOAN_APPROVE = "loan.approve", "Approve loan"
        LOAN_DISBURSE = "loan.disburse", "Disburse loan"
        DIVIDEND_POST = "dividend.post", "Post dividend"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    action = models.CharField(max_length=20, choices=Action.choices)
    object_id = models.PositiveIntegerField()
    summary = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="requested_approvals")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name="decided_approvals")
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_action_display()} #{self.object_id} ({self.status})"
