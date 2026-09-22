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

import logging

from django.contrib import admin

logger = logging.getLogger("api.errors")


class CooperativeOSAdminSite(admin.AdminSite):
    # The branding block in templates/admin/base_site.html renders the
    # wordmark and its "Platform administration" subtitle, so site_header is
    # just the product name.
    site_title = "CooperativeOS admin"
    site_header = "CooperativeOS"
    index_title = "Platform operations"

    def each_context(self, request):
        """Add the uploaded platform branding to every admin page.

        Without this the header can only render the built-in mark — the
        template has no way to reach PlatformProfile on its own.
        """
        context = super().each_context(request)
        context.update(self._branding())
        return context

    def _branding(self) -> dict:
        """Logo and name from the platform profile.

        Never fatal. The admin has to render before the first migration runs
        (the table may not exist yet) and on the login page, which anonymous
        visitors reach. Deliberately uses ``.first()`` rather than
        ``PlatformProfile.load()``: ``load`` does a get_or_create, and writing
        a row on every page render — including anonymous ones — is not
        something a GET should do.
        """
        fallback = {"platform_logo": "", "platform_name": self.site_header}
        try:
            from platform_admin.models import PlatformProfile

            profile = PlatformProfile.objects.first()
            if profile is None:
                return fallback
            return {
                "platform_logo": profile.logo.url if profile.logo else "",
                "platform_name": profile.name or self.site_header,
            }
        except Exception:  # noqa: BLE001 - branding must never break the admin
            logger.warning("Could not load platform branding for the admin",
                           exc_info=True)
            return fallback

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
