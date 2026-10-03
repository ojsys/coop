"""
Member photographs and KYC documents must not be publicly readable.

``/media/`` used to be served wide open, so anyone with — or guessing — a URL
could fetch a member's photograph or their means of ID. Those are NDPA-regulated
personal data, and DEPLOY_CPANEL.md recorded it as a blocker before onboarding a
live society.

The fix is a split: public prefixes (logos, favicons, site photographs, the APK)
stay open because the marketing site serves them to anonymous visitors, while
``member_photos/`` and ``member_documents/`` are released only through a signed,
expiring URL minted by ``core.media`` — or to a staff admin session, because
reviewing KYC in the Django admin is a real workflow and that path carries a
genuine session identity.

The cases worth reading are the ones that encode the reasoning:

* a signed token releases **one** file, and only under a private prefix, so it
  is not a general read capability over MEDIA_ROOT;
* the filename in the URL is decoration — the token decides what is served;
* ``MEDIA_ROOT`` is resolved per request, not when the URLconf was imported,
  which is what makes any of this testable against ``tmp_path``.
"""
from __future__ import annotations

import io
from urllib.parse import urlparse

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant
from core.media import sign_name, signed_media_url

pytestmark = pytest.mark.django_db

PUBLIC = "site_content/hero.png"
DOCUMENT = "member_documents/nin.pdf"
PHOTO = "member_photos/face.png"


@pytest.fixture
def media(tmp_path, settings):
    """Point MEDIA_ROOT at a temp dir.

    This only works because the media views resolve ``settings.MEDIA_ROOT``
    when they run. The old route bound ``document_root`` at import time — see
    the note in tests/test_deployment.py — so an override like this never
    reached it.
    """
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _write(media, name: str, body: bytes = b"a private document") -> str:
    path = media / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return name


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (11, 79, 58)).save(buffer, format="PNG")
    return buffer.getvalue()


def _body(response) -> bytes:
    if getattr(response, "streaming", False):
        return b"".join(response.streaming_content)
    return response.content


def _api(user) -> APIClient:
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _signed_url(name: str, filename: str | None = None) -> str:
    return f"/media/private/{sign_name(name)}/{filename or name.rsplit('/', 1)[-1]}"


# ── Public media is unaffected ──────────────────────────────────────────────
def test_public_media_is_still_served_anonymously(client, media):
    """The marketing site's photographs must keep working for visitors."""
    _write(media, PUBLIC, b"hero image")

    response = client.get(f"/media/{PUBLIC}")

    assert response.status_code == 200
    assert b"hero image" in _body(response)


# ── Private media is not ────────────────────────────────────────────────────
def test_a_kyc_document_is_not_publicly_readable(client, media):
    _write(media, DOCUMENT)

    response = client.get(f"/media/{DOCUMENT}")

    assert response.status_code == 404, (
        "a means-of-ID document must not be fetchable by URL alone")


def test_a_member_photograph_is_not_publicly_readable(client, media):
    _write(media, PHOTO, _png())

    assert client.get(f"/media/{PHOTO}").status_code == 404


def test_the_404_does_not_distinguish_a_missing_file_from_a_withheld_one(
        client, media):
    """A document's existence is itself information."""
    _write(media, DOCUMENT)

    withheld = client.get(f"/media/{DOCUMENT}")
    missing = client.get("/media/member_documents/does-not-exist.pdf")

    assert withheld.status_code == missing.status_code == 404


# ── Signed URLs ─────────────────────────────────────────────────────────────
def test_a_signed_url_serves_the_file(client, media):
    _write(media, DOCUMENT, b"nin slip")

    response = client.get(_signed_url(DOCUMENT))

    assert response.status_code == 200
    assert b"nin slip" in _body(response)


def test_a_signed_response_is_not_cached_by_shared_proxies_or_indexed(
        client, media):
    _write(media, DOCUMENT)

    response = client.get(_signed_url(DOCUMENT))

    assert "private" in response["Cache-Control"]
    assert "noindex" in response["X-Robots-Tag"]


def test_a_tampered_token_is_refused(client, media):
    _write(media, DOCUMENT)
    url = _signed_url(DOCUMENT)
    token = url.split("/")[3]
    tampered = url.replace(token, token[:-4] + "xxxx")

    assert client.get(tampered).status_code == 404


def test_an_expired_signature_is_refused(client, media, settings):
    """The whole point of signing with an expiry rather than a secret path."""
    _write(media, DOCUMENT)
    url = _signed_url(DOCUMENT)
    assert client.get(url).status_code == 200

    settings.PRIVATE_MEDIA_TTL = -1

    assert client.get(url).status_code == 404


def test_a_token_cannot_release_a_file_outside_the_private_prefixes(
        client, media):
    """Otherwise a token would be a general read capability over MEDIA_ROOT."""
    _write(media, PUBLIC, b"hero image")

    response = client.get(_signed_url(PUBLIC))

    assert response.status_code == 404


def test_a_token_cannot_escape_media_root(client, media, tmp_path):
    """Defence in depth: the name comes from our own signature, but a traversal
    bug here would read as 'any file on the server'."""
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("not for you")

    response = client.get(_signed_url("member_documents/../outside.txt",
                                      filename="outside.txt"))

    assert response.status_code == 404


def test_the_filename_segment_cannot_change_which_file_is_served(client, media):
    """The filename is decoration so downloads keep a sensible name; the token
    alone decides the file."""
    _write(media, DOCUMENT, b"nin slip")
    _write(media, "member_documents/other.pdf", b"someone else")

    response = client.get(_signed_url(DOCUMENT, filename="other.pdf"))

    assert response.status_code == 200
    assert b"nin slip" in _body(response), "the token must win, not the filename"


# ── The admin workflow ──────────────────────────────────────────────────────
def test_staff_with_a_session_may_fetch_private_media_directly(client, media):
    """Django admin renders plain /media/ links for a member's documents, and
    reviewing KYC there is a real workflow. That path is session-authenticated."""
    _write(media, DOCUMENT, b"nin slip")
    operator = User.objects.create_superuser(
        email="ops@startupripple.co", password="pw-test-12345",
        full_name="Platform Operator",
    )
    client.force_login(operator)

    response = client.get(f"/media/{DOCUMENT}")

    assert response.status_code == 200
    assert b"nin slip" in _body(response)


def test_an_ordinary_signed_in_member_still_cannot(client, media, coop):
    """Being signed in is not the same as being staff."""
    _write(media, DOCUMENT)
    with use_tenant(coop):
        user = User.objects.create_user(
            email="rank@imole.coop", full_name="Rank Member", password="pw")
        Membership.objects.create(
            user=user, member_no="IMC-9001",
            role=Role.objects.filter(slug="member").first())
    client.force_login(user)

    assert client.get(f"/media/{DOCUMENT}").status_code == 404


# ── What the API hands clients ──────────────────────────────────────────────
def test_the_helper_signs_private_files_and_leaves_public_ones_alone(
        coop, member, media):
    with use_tenant(coop):
        member.photo = SimpleUploadedFile("face.png", _png(),
                                          content_type="image/png")
        member.save(update_fields=["photo"])
        coop.logo = SimpleUploadedFile("logo.png", _png(),
                                       content_type="image/png")
        coop.save(update_fields=["logo"])

    assert signed_media_url(member.photo).startswith("/media/private/")
    assert "/private/" not in signed_media_url(coop.logo)


def test_me_profile_returns_a_signed_photo_url(coop, member, media):
    """MemberSelfSerializer inherits the rewrite, so /me/profile/ is covered."""
    client = _api(member.user)
    photo = SimpleUploadedFile("face.png", _png(), content_type="image/png")

    assert client.patch("/api/v1/me/profile/", {"photo": photo},
                        format="multipart",
                        HTTP_X_COOPERATIVE_ID=str(coop.id)).status_code == 200

    body = client.get("/api/v1/me/profile/",
                      HTTP_X_COOPERATIVE_ID=str(coop.id)).json()
    assert "/media/private/" in body["photo"]
    assert "member_photos/" not in body["photo"]
    # And the URL it handed back actually works.
    assert APIClient().get(urlparse(body["photo"]).path).status_code == 200


def test_the_api_keeps_the_url_absolute(coop, member, media):
    """DRF's FileField returns an absolute URL when the context has a request.

    Dropping to a relative one would keep the browser working — it resolves
    relative hrefs — while silently breaking the Flutter client, which loads the
    photograph with NetworkImage and cannot resolve a relative path. The failure
    would have read as "member photos don't load on Android".
    """
    client = _api(member.user)
    photo = SimpleUploadedFile("face.png", _png(), content_type="image/png")
    client.patch("/api/v1/me/profile/", {"photo": photo}, format="multipart",
                 HTTP_X_COOPERATIVE_ID=str(coop.id))

    body = client.get("/api/v1/me/profile/",
                      HTTP_X_COOPERATIVE_ID=str(coop.id)).json()

    assert urlparse(body["photo"]).scheme in ("http", "https")
    assert urlparse(body["photo"]).netloc


def test_me_documents_returns_a_signed_file_url(coop, member, media):
    client = _api(member.user)
    doc = SimpleUploadedFile("nin.pdf", b"nin slip",
                             content_type="application/pdf")

    created = client.post("/api/v1/me/documents/",
                          {"doc_type": "id", "file": doc}, format="multipart",
                          HTTP_X_COOPERATIVE_ID=str(coop.id))

    assert created.status_code == 201, created.content
    assert "/media/private/" in created.json()["file"]


def test_a_loan_does_not_leak_the_member_photograph(coop, member, media):
    """The loans API exposes member_photo through its own method field, so it
    needs the same treatment — patching only accounts/serializers.py would have
    left the photograph readable here."""
    from loans.models import Loan, LoanProduct

    with use_tenant(coop):
        member.photo = SimpleUploadedFile("face.png", _png(),
                                          content_type="image/png")
        member.save(update_fields=["photo"])
        product = LoanProduct.objects.create(
            name="Emergency", interest_rate="10.00", max_amount="500000.00")
        Loan.objects.create(
            membership=member, product=product, principal="100000.00",
            interest_rate="10.00", term_months=12)

    rows = _api(member.user).get(
        "/api/v1/me/loans/", HTTP_X_COOPERATIVE_ID=str(coop.id)).json()
    results = rows["results"] if isinstance(rows, dict) else rows

    assert results, "expected the member's own loan"
    photo = results[0]["member_photo"]
    assert photo and "/media/private/" in photo
    assert "member_photos/" not in photo
