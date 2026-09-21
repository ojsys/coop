"""
AppConfig that installs the CooperativeOS admin site.

Listed in ``INSTALLED_APPS`` in place of ``django.contrib.admin``; it configures
that same app (``AdminConfig.name`` is ``django.contrib.admin``), pointing it at
our ``AdminSite`` subclass.

This lives in its own module rather than in ``core/apps.py`` on purpose: Django
resolves the plain ``'core'`` entry by scanning ``core.apps`` for ``AppConfig``
subclasses, and an admin config sitting there — or even the imported
``AdminConfig`` symbol — confuses that scan into registering a second app
labelled ``admin``.

``default_site`` is a dotted *string* so the site class is imported lazily,
after the app registry is ready.
"""
from __future__ import annotations

from django.contrib.admin.apps import AdminConfig


class CooperativeOSAdminConfig(AdminConfig):
    default_site = "core.admin_site.CooperativeOSAdminSite"
