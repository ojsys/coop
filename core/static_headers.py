"""Cache headers for the SPA build that WhiteNoise serves.

WhiteNoise decides how long to cache a file with ``immutable_file_test``, which
only recognises files under ``STATIC_URL`` (``/static/``). The Vite build is
served from the *URL root* via ``WHITENOISE_ROOT``, so nothing in it ever
matches and every file falls back to ``WHITENOISE_MAX_AGE``.

That is wrong in both directions at once:

* ``/assets/index-<hash>.js`` is content-addressed — its name changes whenever
  its contents do — so it could be cached forever, but gets an hour.
* ``sw.js``, ``registerSW.js`` and ``manifest.webmanifest`` carry no hash and
  decide *which build the browser actually runs*. Caching those strands
  clients: the service worker keeps serving its own precached shell, which
  points at the previous bundle, so a deploy appears to do nothing no matter
  how many times the app is restarted.

This hook is invoked last in ``whitenoise.base.get_static_file`` — after
``add_cache_headers`` — so the values set here win.
"""
from __future__ import annotations

import re

# Files that determine which build runs. Always revalidate.
NEVER_CACHE = frozenset({
    "/sw.js",
    "/registerSW.js",
    "/manifest.webmanifest",
    "/index.html",
})

# Vite emits /assets/<name>-<hash>.<ext>. The hash makes the URL unique per
# build, so a stale copy is impossible by construction.
HASHED_ASSET = re.compile(r"^/assets/.+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")

NO_CACHE = "no-cache, no-store, must-revalidate"
IMMUTABLE = "max-age=31536000, public, immutable"


def add_spa_cache_headers(headers, path, url) -> None:
    """Set Cache-Control for one file WhiteNoise is about to serve.

    Signature is fixed by WhiteNoise: ``(headers, path, url)``. Anything not
    matched here keeps whatever WhiteNoise already decided, so this only ever
    narrows the two cases that matter.
    """
    if url in NEVER_CACHE:
        headers["Cache-Control"] = NO_CACHE
    elif HASHED_ASSET.match(url):
        headers["Cache-Control"] = IMMUTABLE
