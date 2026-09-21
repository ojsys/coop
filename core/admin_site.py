"""
The CooperativeOS admin site.

Django's default ``AdminSite`` only requires ``is_staff``. The admin here reads
*across every tenant* (see :mod:`core.admin`), so plain staff access is too
broad: a staff user granted "can change membership" would see every
cooperative's members. This site narrows entry to the platform operators the
cross-tenant view is intended for.

Wired in via ``core.apps.CooperativeOSAdminConfig`` (``default_site``), which is
listed in ``INSTALLED_APPS`` in place of ``django.contrib.admin``. The class is
referenced lazily by dotted path so no model is imported during app loading.
"""
from __future__ import annotations

from django.contrib import admin


class CooperativeOSAdminSite(admin.AdminSite):
    # The branding block in templates/admin/base_site.html renders the
    # wordmark and its "Platform administration" subtitle, so site_header is
    # just the product name.
    site_title = "CooperativeOS admin"
    site_header = "CooperativeOS"
    index_title = "Platform operations"

    def has_permission(self, request):
        """Allow only active platform operators.

        Tightens ``AdminSite.has_permission`` (``is_active and is_staff``) with
        the platform-admin flag. To open the admin to ordinary staff, drop the
        final check — but revisit the cross-tenant querysets in
        ``core.admin.UnscopedAdminMixin`` first, since they are what make this
        restriction necessary.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_active:
            return False
        if not user.is_staff:
            return False
        return bool(getattr(user, "is_platform_admin", False) or user.is_superuser)
