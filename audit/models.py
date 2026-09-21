"""
Immutable audit log (PRD §6.5): who did what, when, and the before/after state
of the affected entity. Append-only and tamper-evident — a compliance asset for
regulators and development partners.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

from core.models import AppendOnlyModel, TenantManager, TimeStampedModel


class AuditLog(AppendOnlyModel, TimeStampedModel):
    cooperative = models.ForeignKey(
        "tenants.Cooperative", on_delete=models.CASCADE,
        null=True, blank=True, related_name="audit_logs", db_index=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="audit_actions",
    )
    # Free-text actor label (e.g. "System") when no user is attached.
    actor_label = models.CharField(max_length=120, blank=True)
    action = models.CharField(max_length=160)
    entity_type = models.CharField(max_length=80, blank=True)
    entity_id = models.CharField(max_length=64, blank=True)
    entity_repr = models.CharField(max_length=255, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["cooperative", "-created_at"])]

    def __str__(self) -> str:
        who = self.actor_label or (self.actor and self.actor.full_name) or "?"
        return f"{who}: {self.action}"
