"""
Deployment system checks.

A missing SPA build is otherwise a *silent* failure: WhiteNoise logs a single
``UserWarning: No directory at: ...`` to stderr, the app boots normally, the
admin and API work — and every non-API route returns 404 with nothing obvious
to point at. Surfacing it through ``manage.py check`` turns that into something
you see before restarting, rather than after.
"""
from __future__ import annotations

from pathlib import Path

from django.core.checks import Warning, register


@register()
def frontend_build_present(app_configs, **kwargs):
    """Warn when FRONTEND_DIST has no index.html to serve."""
    from django.conf import settings

    dist = Path(settings.FRONTEND_DIST)
    if (dist / "index.html").is_file():
        return []

    return [
        Warning(
            f"No SPA build at {dist}",
            hint=(
                'Django serves "/" from this directory, so every non-API route '
                "will return 404 until it exists. Build it with `npm run build` "
                "in frontend/, upload the output, and either place it at one of "
                "the default locations or set the FRONTEND_DIST environment "
                "variable to where it actually lives."
            ),
            id="core.W001",
        )
    ]
