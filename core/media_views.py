"""
Serving uploaded media, with private prefixes gated.

Two views, both reached from config/urls.py:

``serve_private_media``
    ``/media/private/<token>/<filename>`` — verifies a signed capability from
    ``core.media`` and streams the file. No authentication of its own: the
    signature *is* the authorisation (see that module for why, and for the
    trade-off this accepts).

``serve_media``
    ``/media/<path>`` — the ordinary public route, which now refuses the private
    prefixes. Staff with an admin **session** are still allowed through, because
    Django's admin renders plain ``/media/...`` links for a member's photograph
    and documents and reviewing KYC in the admin is a real workflow. That path
    is session-authenticated, so it is a genuine identity check rather than a
    hole.

Both resolve ``MEDIA_ROOT`` at call time, not import time. The existing route
bound ``document_root`` when the URLconf was imported, which is why
tests/test_deployment.py notes that overriding the setting afterwards "would not
reach the view" — doing the same here would make this untestable with tmp_path.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.views.decorators.cache import never_cache
from django.views.static import serve as django_serve

from core.media import is_private, unsign_name


def _resolved_under_media_root(name: str) -> Path:
    """Resolve ``name`` inside MEDIA_ROOT, refusing anything that escapes it.

    Defence in depth. The name comes out of a signature we minted, so it should
    already be safe — but a traversal bug here would be readable as "any file on
    the server", so it is checked rather than trusted.
    """
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise Http404
    return target


@never_cache
def serve_private_media(request, token: str, filename: str | None = None):
    """Stream a private file for a valid, unexpired signed token."""
    name = unsign_name(token)
    if name is None:
        # Forged, tampered or expired — all answered identically, and without
        # revealing whether the underlying file exists.
        raise Http404

    # A token may only ever release a file under a private prefix. Without this
    # a token would be a general-purpose read capability over MEDIA_ROOT.
    if not is_private(name):
        raise Http404

    target = _resolved_under_media_root(name)
    if not target.is_file():
        raise Http404

    response = FileResponse(target.open("rb"), as_attachment=False,
                            filename=target.name)
    # Cacheable by the member's own browser for the life of the signature, but
    # never by a shared proxy, and never indexed.
    response["Cache-Control"] = "private, max-age=300"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def serve_media(request, path: str):
    """The public ``/media/`` route, with private prefixes withheld."""
    if is_private(path):
        user = getattr(request, "user", None)
        staff = bool(
            user
            and user.is_authenticated
            and (user.is_staff or getattr(user, "is_platform_admin", False))
        )
        if not staff:
            # 404 rather than 403: a member document's existence is itself
            # information, and this URL used to be public, so anything already
            # circulating should simply stop resolving.
            raise Http404

    return django_serve(request, path, document_root=settings.MEDIA_ROOT)
