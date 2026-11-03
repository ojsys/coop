"""
The picture shown when a link to the site is shared (WhatsApp, Telegram,
Facebook, LinkedIn, X).

Built from the logo uploaded under Platform Settings → Branding, so changing
the logo changes the preview without a rebuild. The card's text and
background are a fixed image (``assets/share-card-base.png``); only the logo
is pasted in, so the server needs no fonts. With no logo uploaded, or one
that cannot be read, the bundled CooperativeOS logo is used.

Chat apps cache a preview's image by its URL for weeks, so the URL carries a
version that changes with the logo (:func:`card_version`), and the SPA shell
points at it (core.views_web.spa_index).
"""
from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

from django.core.cache import cache
from PIL import Image

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets"
CARD_PATH = "/share/card.jpg"
# Where the logo sits on the card: a square left of the text.
SLOT = (110, 125, 490, 505)


def _profile():
    """The platform profile, or None. Never writes a row: this runs on every
    page load, including anonymous ones (see core.admin_site)."""
    try:
        from platform_admin.models import PlatformProfile

        return PlatformProfile.objects.only("logo", "updated_at").first()
    except Exception:  # noqa: BLE001 - a preview must never break the page
        logger.warning("Could not load the platform profile", exc_info=True)
        return None


def card_version(profile=None) -> str:
    """Changes whenever the uploaded logo does."""
    profile = profile if profile is not None else _profile()
    logo = profile.logo.name if profile and profile.logo else ""
    stamp = profile.updated_at.isoformat() if profile and logo else ""
    return hashlib.sha1(f"{logo}|{stamp}".encode()).hexdigest()[:10]


def _logo(profile) -> Image.Image:
    if profile and profile.logo:
        try:
            with profile.logo.open("rb") as fh:
                image = Image.open(fh)
                image.load()
            return image.convert("RGBA")
        except Exception:  # noqa: BLE001 - fall back to the bundled logo
            logger.warning("Could not read the uploaded logo for the share "
                           "card", exc_info=True)
    return Image.open(ASSETS / "default-logo.png").convert("RGBA")


def render_card(profile) -> bytes:
    """The card as a JPEG: WhatsApp skips some PNGs, and JPEG is safe
    everywhere."""
    card = Image.open(ASSETS / "share-card-base.png").convert("RGB")
    logo = _logo(profile)
    left, top, right, bottom = SLOT
    logo.thumbnail((right - left, bottom - top), Image.LANCZOS)
    # Centred in the slot, whatever the logo's shape.
    x = left + (right - left - logo.width) // 2
    y = top + (bottom - top - logo.height) // 2
    card.paste(logo, (x, y), logo)
    out = io.BytesIO()
    card.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def card_bytes() -> bytes:
    """The current card, rendered once per logo."""
    profile = _profile()
    key = f"share-card:{card_version(profile)}"
    data = cache.get(key)
    if data is None:
        data = render_card(profile)
        cache.set(key, data, 60 * 60 * 24)
    return data
