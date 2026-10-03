"""
Signed, expiring URLs for private uploaded media.

``/media/`` is served by Django (see config/urls.py) and was historically open:
anyone with — or guessing — a URL could fetch a member photograph or a KYC
document. Those are NDPA-regulated personal data. This module is the first half
of closing that; ``core.media_views`` is the second.

Why signed URLs rather than an authenticated view
-------------------------------------------------
The SPA and the Flutter app authenticate with a DRF **token** in the
``Authorization`` header. A browser never sends that header for ``<img src>`` or
for a link opened in a new tab, so a ``login_required``-style gate would make
every photograph and document link fail — which is exactly why the original
note in DEPLOY_CPANEL.md said this "needs a real design, not a one-line change".

So authorisation stays where it already works: the API. A caller who is allowed
to read a membership gets back a URL carrying a signed, time-limited capability
for that one file. The media view then needs no identity of its own.

The trade this accepts, stated plainly: for the life of the signature the URL
*is* the authorisation, so anyone it is forwarded to can fetch the file until it
expires. That is the same model as an S3 pre-signed URL. It is bounded by
``PRIVATE_MEDIA_TTL`` and the signature is not guessable; it is a large
improvement on "public forever" and not the same thing as per-request identity.

Not all media is private
------------------------
Cooperative logos, favicons, platform branding, site-content photographs and the
uploaded APK are *meant* to be public — they appear on the marketing site to
anonymous visitors. Only the prefixes in ``settings.PRIVATE_MEDIA_PREFIXES`` are
gated, so gating does not break the public site.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from django.conf import settings
from django.core import signing

# Namespaces the signature. A token minted for media cannot be replayed against
# any other signing use in the project, and vice versa.
SALT = "core.media.private"


def private_prefixes() -> tuple[str, ...]:
    return tuple(getattr(settings, "PRIVATE_MEDIA_PREFIXES", ()))


def ttl_seconds() -> int:
    return int(getattr(settings, "PRIVATE_MEDIA_TTL", 3600))


def is_private(name: str | None) -> bool:
    """Whether a stored file name falls under a gated prefix."""
    if not name:
        return False
    return str(name).lstrip("/").startswith(private_prefixes())


def media_url_base() -> str:
    """``MEDIA_URL`` as a root-relative path.

    ``settings.MEDIA_URL`` is ``'media/'`` here — no leading slash — so it must
    be normalised before being handed to a client as a URL.
    """
    return "/" + str(settings.MEDIA_URL).strip("/") + "/"


def sign_name(name: str) -> str:
    """Mint a capability token for one stored file."""
    return signing.dumps({"p": str(name).lstrip("/")}, salt=SALT)


def unsign_name(token: str) -> str | None:
    """Recover the file name from a token, or None if invalid or expired.

    Returns None rather than raising so callers answer 404 uniformly: an
    expired token and a forged one should be indistinguishable to a client, and
    neither should confirm whether a file exists.
    """
    try:
        payload = signing.loads(token, salt=SALT, max_age=ttl_seconds())
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("p")
    return str(name).lstrip("/") if name else None


def signed_media_url(file_field) -> str | None:
    """The URL to hand a client for a (possibly private) uploaded file.

    Public prefixes keep their ordinary ``/media/...`` URL — they are cacheable
    and shared with anonymous visitors. Private ones become
    ``/media/private/<token>/<filename>``.

    The original filename is kept as the last segment deliberately: a saved
    document keeps a meaningful name, and the path still reads as a file rather
    than an opaque blob. It is decorative only — the token alone decides what is
    served, so altering the filename cannot reach a different file.
    """
    if not file_field:
        return None

    name = getattr(file_field, "name", None)
    if not name:
        return None

    if not is_private(name):
        try:
            return file_field.url
        except ValueError:
            return None

    filename = PurePosixPath(name).name
    return f"{media_url_base()}private/{sign_name(name)}/{filename}"


def sign_private_urls(data: dict, *fields: str, request=None) -> dict:
    """Rewrite named fields of a serialized dict to signed URLs, in place.

    Used from ``to_representation`` so that ``photo`` and ``file`` stay
    *writable* for multipart upload while only their output changes. Every
    consumer — the console, the member app, the Flutter client — reads whatever
    string the API returns, so this needs no client change.

    ``request`` must be passed when one is available, because DRF's own
    ``FileField.to_representation`` returns an **absolute** URL whenever the
    serializer context carries a request. Dropping to a relative URL here would
    keep the web app working (browsers resolve relative hrefs) while silently
    breaking the Flutter client, which loads the photograph through
    ``NetworkImage`` and needs an absolute URL. The bug would have looked like
    "member photos fail on Android only".

    A field already holding a signed URL (or empty) is left alone.
    """
    base = media_url_base()

    for field in fields:
        value = data.get(field)
        if not value or "/private/" in str(value):
            continue

        # The serialized value is a URL — absolute or relative. Recover the
        # stored name that sits beneath MEDIA_URL.
        name = str(value)
        stored = name.split(base, 1)[1] if base in name else name.lstrip("/")
        if not is_private(stored):
            continue

        url = f"{base}private/{sign_name(stored)}/{PurePosixPath(stored).name}"
        data[field] = request.build_absolute_uri(url) if request else url

    return data
