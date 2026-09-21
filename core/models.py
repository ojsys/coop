"""
Base classes for tenant-scoped models.

Every model that stores cooperative-owned data inherits from
:class:`TenantScopedModel`, which:

* carries a mandatory ``cooperative`` FK (the row-level tenant key), and
* exposes ``objects`` — a manager that *automatically* filters to the active
  tenant (from :mod:`core.context`) so a query can never accidentally read
  across cooperatives.

``all_objects`` is an unscoped escape hatch reserved for platform super-admin
tooling and migrations; application code should always use ``objects``.
"""
from __future__ import annotations

from django.db import models

from core.context import get_current_cooperative


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, cooperative):
        return self.filter(cooperative=cooperative)


class TenantManager(models.Manager):
    """Manager that scopes every query to the active cooperative.

    When no tenant is bound (e.g. platform-level code that has not entered a
    tenant context) the manager returns an *empty* queryset by default, so a
    forgotten ``use_tenant`` fails closed rather than leaking data. Use
    ``all_objects`` for deliberately cross-tenant access.
    """

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db)
        cooperative = get_current_cooperative()
        if cooperative is None:
            return qs.none()
        return qs.filter(cooperative=cooperative)


class TenantScopedModel(models.Model):
    cooperative = models.ForeignKey(
        "tenants.Cooperative",
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        db_index=True,
    )

    # Tenant-scoped by default; ``all_objects`` bypasses scoping.
    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        # Fail-safe: if a row is created inside a tenant context without an
        # explicit cooperative, inherit the active one.
        if self.cooperative_id is None:
            current = get_current_cooperative()
            if current is not None:
                self.cooperative = current
        super().save(*args, **kwargs)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ImmutableRecordError(Exception):
    """Raised on any attempt to mutate or delete an append-only record."""


class AppendOnlyModel(models.Model):
    """Mixin for records that may be inserted but never updated or deleted.

    Used by the ledger and the audit log — anything that must be tamper-evident.
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(
                f"{type(self).__name__} is append-only and cannot be modified."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(
            f"{type(self).__name__} is append-only and cannot be deleted."
        )
