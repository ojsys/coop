"""
Shared admin base classes for tenant-scoped and append-only models.

**Why these exist.** Tenant-scoped models declare ``objects = TenantManager()``
first, so ``_default_manager`` is the *scoping* manager: it filters to the
cooperative bound to the current thread and returns an empty queryset when none
is bound. Admin requests never carry the ``X-Cooperative-ID`` header, and
``core.tenancy.resolve_cooperative`` returns ``None`` for a platform admin
without one — so a plain ``admin.site.register(Model)`` would render every
tenant-scoped changelist, and every related dropdown, *completely empty*.

The admin is a cross-tenant, platform-operator tool, so it deliberately reads
through the unscoped ``all_objects`` escape hatch and surfaces ``cooperative``
as a column/filter instead. Entry is restricted to platform admins by
``core.admin_site.CooperativeOSAdminSite``, and writes to money-touching models
additionally require 2FA (:class:`TwoFactorRequiredMixin`).
"""
from __future__ import annotations

from django.contrib import admin


class UnscopedAdminMixin:
    """Read through ``all_objects`` instead of the tenant-scoping manager.

    Mixed in *before* ``ModelAdmin``/``InlineModelAdmin`` so it takes precedence
    over ``BaseModelAdmin.get_queryset``, which would otherwise use
    ``_default_manager`` (the scoping one).
    """

    def get_queryset(self, request):
        manager = getattr(self.model, "all_objects", None)
        if manager is None:
            return super().get_queryset(request)
        # Mirrors BaseModelAdmin.get_queryset, but unscoped.
        qs = manager.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Populate FK dropdowns from the related model's unscoped manager.

        Without this, choosing (say) a Membership would offer an empty list for
        exactly the same reason the changelist would be empty.
        """
        related = db_field.remote_field.model
        if "queryset" not in kwargs and hasattr(related, "all_objects"):
            kwargs["queryset"] = related.all_objects.get_queryset()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class TwoFactorActionsMixin:
    """Hide money-moving admin *actions* from operators without 2FA."""

    #: Names of actions that move money and must be hidden without 2FA.
    two_factor_actions: tuple[str, ...] = ()

    @staticmethod
    def has_two_factor(request) -> bool:
        return bool(getattr(request.user, "two_factor_enabled", False))

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not self.has_two_factor(request):
            for name in self.two_factor_actions:
                actions.pop(name, None)
        return actions


class TwoFactorRequiredMixin(TwoFactorActionsMixin):
    """Require 2FA before an operator may write to a money-touching model.

    ``accounts.Role.PRIVILEGED_PERMISSIONS`` declares that ledger, member and
    governance management require 2FA before handling live money. The admin is
    the most privileged surface in the product, so it honours the same rule:
    *viewing* is always allowed, but add/change/delete require
    ``User.two_factor_enabled``.

    Superusers are deliberately **not** exempt — a blanket exemption would void
    the control precisely where it matters most. ``UserAdmin`` deliberately does
    *not* use this mixin: an operator must be able to turn their own 2FA on,
    otherwise a superuser without it would be locked out permanently.
    """

    def has_add_permission(self, request):
        return (super().has_add_permission(request)
                and self.has_two_factor(request))

    def has_change_permission(self, request, obj=None):
        return (super().has_change_permission(request, obj)
                and self.has_two_factor(request))

    def has_delete_permission(self, request, obj=None):
        return (super().has_delete_permission(request, obj)
                and self.has_two_factor(request))


class TenantScopedModelAdmin(UnscopedAdminMixin, admin.ModelAdmin):
    """Base for any model inheriting ``core.models.TenantScopedModel``."""


class TenantScopedTabularInline(UnscopedAdminMixin, admin.TabularInline):
    """Tabular inline for tenant-scoped children."""

    def get_queryset(self, request):
        # The mixin replaces InlineModelAdmin.get_queryset, so re-apply the
        # per-request permission narrowing it would otherwise have done.
        qs = super().get_queryset(request)
        if not self.has_view_or_change_permission(request):
            qs = qs.none()
        return qs


class TenantScopedStackedInline(UnscopedAdminMixin, admin.StackedInline):
    """Stacked inline for tenant-scoped children."""

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not self.has_view_or_change_permission(request):
            qs = qs.none()
        return qs


class AppendOnlyAdmin(TenantScopedModelAdmin):
    """Strictly read-only admin for append-only (tamper-evident) records.

    The underlying models raise ``ImmutableRecordError`` on any update or
    delete, so rather than let a save blow up in the form we remove the add,
    change and delete affordances entirely. Superusers keep *view* access, which
    is the whole point of an audit trail. Corrections are made by posting a new
    record (e.g. ``ledger.services.reverse_journal``), never by editing one.
    """

    def get_readonly_fields(self, request, obj=None):
        declared = tuple(super().get_readonly_fields(request, obj))
        concrete = tuple(f.name for f in self.model._meta.fields)
        return tuple(dict.fromkeys(concrete + declared))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
