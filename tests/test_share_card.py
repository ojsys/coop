"""The link-preview card is built from the uploaded platform logo."""
from __future__ import annotations

import io

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import Client
from PIL import Image

from core.views_web import STATIC_CARD
from platform_admin.models import PlatformProfile

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        f'<meta property="og:image" content="{STATIC_CARD}" />'
        f'<meta name="twitter:image" content="{STATIC_CARD}" />')
    settings.FRONTEND_DIST = dist
    cache.clear()


def _png(colour, size=(300, 300)) -> ContentFile:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return ContentFile(out.getvalue(), name="logo.png")


def _card() -> Image.Image:
    resp = Client().get("/share/card.jpg")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/jpeg"
    return Image.open(io.BytesIO(resp.content))


def _logo_centre(card):
    return card.convert("RGB").getpixel((300, 315))


def test_the_card_is_a_1200_by_630_jpeg_with_the_default_logo():
    card = _card()
    assert card.format == "JPEG" and card.size == (1200, 630)
    # The bundled logo's centre: its light grey dot.
    r, g, b = _logo_centre(card)
    assert r > 200 and g > 200 and b > 200


def test_the_card_uses_the_uploaded_logo():
    profile = PlatformProfile.load()
    profile.logo.save("logo.png", _png((200, 0, 0)))
    r, g, b = _logo_centre(_card())
    assert r > 180 and g < 40 and b < 40


def test_the_page_points_at_a_url_that_changes_with_the_logo():
    first = Client().get("/").content.decode()
    assert STATIC_CARD not in first
    assert first.count("http://testserver/share/card.jpg?v=") == 2

    profile = PlatformProfile.load()
    profile.logo.save("logo.png", _png((0, 0, 200)))
    second = Client().get("/").content.decode()
    version = lambda html: html.split("?v=")[1].split('"')[0]  # noqa: E731
    assert version(first) != version(second)


def test_an_unreadable_logo_falls_back_to_the_default():
    profile = PlatformProfile.load()
    profile.logo.save("logo.png", ContentFile(b"not an image", name="x.png"))
    r, g, b = _logo_centre(_card())
    assert r > 200 and g > 200 and b > 200


def test_a_wide_logo_fits_inside_the_slot():
    profile = PlatformProfile.load()
    profile.logo.save("logo.png", _png((200, 0, 0), size=(1600, 400)))
    card = _card().convert("RGB")
    assert card.getpixel((300, 315))[0] > 180
    # Nothing spills over the text column.
    assert card.getpixel((540, 315)) == pytest.approx((244, 241, 234), abs=6)
