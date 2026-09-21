"""
Serving the built React SPA from Django.

On a single-origin deployment (one cPanel domain) Django is the only thing
listening, so it must hand out the SPA's ``index.html`` for every non-API route.
The client's axios ``baseURL`` is a relative ``/api/v1``, so the app and the API
*must* share an origin — see frontend/src/api/client.ts.

Static assets (``/assets/*``, ``/sw.js``, ``/manifest.webmanifest``) are served
by WhiteNoise straight from the Vite build directory; only unmatched routes
reach this view.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.cache import never_cache


@never_cache
def spa_index(request):
    """Return the SPA shell so client-side routing can take over.

    Never cached: the HTML references content-hashed bundles, so a stale shell
    would pin browsers to a bundle that no longer exists after a redeploy.
    """
    index = Path(settings.FRONTEND_DIST) / "index.html"
    if not index.is_file():
        raise Http404(
            "The frontend build is missing. Run `npm run build` in frontend/ "
            "and upload frontend/dist/ alongside the backend."
        )
    return HttpResponse(index.read_bytes(), content_type="text/html")
