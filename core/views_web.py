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
    html = index.read_text(encoding="utf-8")
    # Link previews show the card built from the uploaded logo, at a URL
    # that changes with it so chat apps fetch the new one (core.share_card).
    from core.share_card import CARD_PATH, card_version
    card = request.build_absolute_uri(f"{CARD_PATH}?v={card_version()}")
    html = html.replace(STATIC_CARD, card)
    return HttpResponse(html, content_type="text/html")


# What the build's index.html names as the preview image; replaced per request.
STATIC_CARD = "https://www.mycooperativeos.com/share-card.jpg"


def share_card(request):
    """`GET /share/card.jpg` — the link-preview picture (core.share_card).

    Cached by browsers and apps for a day; the version in the URL changes
    when the logo does, so a new logo is never stuck behind an old copy.
    """
    from core.share_card import card_bytes

    response = HttpResponse(card_bytes(), content_type="image/jpeg")
    response["Cache-Control"] = "public, max-age=86400"
    return response
