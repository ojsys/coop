"""
Deployment wiring.

These cover the single-origin cPanel layout: Django serves the API, the admin,
uploaded media and the React SPA shell from one domain. The catch-all that
makes SPA routing work is the risky part — it must not swallow anything
server-side — so that boundary is pinned here.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

# Present in the Vite build's index.html; absent from every server-rendered page.
SPA_MARKER = b'id="root"'


def test_frontend_dist_is_present():
    """The SPA shell must ship alongside the backend."""
    index = Path(settings.FRONTEND_DIST) / "index.html"
    assert index.is_file(), (
        f"No SPA build at {index}. Run `npm run build` in frontend/."
    )


def test_missing_build_is_reported_by_manage_py_check(settings):
    """A missing SPA build must be visible, not a buried stderr warning."""
    from core.checks import frontend_build_present

    settings.FRONTEND_DIST = Path("/definitely/not/here")
    warnings = frontend_build_present(None)
    assert [w.id for w in warnings] == ["core.W001"]
    assert "/definitely/not/here" in warnings[0].msg


def test_check_is_silent_when_the_build_is_present():
    from core.checks import frontend_build_present

    assert frontend_build_present(None) == []


def test_default_frontend_dist_finds_a_backend_only_upload(tmp_path):
    """cPanel layout: only backend/ uploaded, build dropped in frontend_dist/."""
    from config.settings.base import _default_frontend_dist

    app_root = tmp_path / "coop"
    (app_root / "frontend_dist").mkdir(parents=True)
    (app_root / "frontend_dist" / "index.html").write_text("<div id=\"root\">")

    assert _default_frontend_dist(app_root) == app_root / "frontend_dist"


def test_spa_catch_all_serves_the_shell(client):
    """A client-side route must resolve on a hard refresh."""
    response = client.get("/app/savings")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert SPA_MARKER in response.content


def test_spa_shell_is_not_cached(client):
    """The shell references content-hashed bundles; caching it strands clients."""
    response = client.get("/login")
    assert "no-cache" in response["Cache-Control"]


# ── Email delivery is configured ────────────────────────────────────────────
# Mail failing is invisible from outside — the reset endpoint answers the same
# way either way — so the check that surfaces it is worth pinning.
SMTP = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE = "django.core.mail.backends.console.EmailBackend"


def test_console_backend_is_never_flagged(settings, monkeypatch):
    """Sending nothing is the point in development."""
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = CONSOLE
    monkeypatch.delenv("DEFAULT_FROM_EMAIL", raising=False)
    assert email_delivery_configured(None) == []


def test_smtp_without_a_host_is_flagged(settings, monkeypatch):
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST = ""
    settings.EMAIL_HOST_USER = "user"
    settings.EMAIL_HOST_PASSWORD = "secret"
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", "ops@example.com")

    ids = [w.id for w in email_delivery_configured(None)]
    assert "core.W002" in ids


def test_smtp_without_credentials_is_flagged(settings, monkeypatch):
    """Brevo needs a login; an empty password fails at AUTH, not at connect."""
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST = "smtp-relay.brevo.com"
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""
    # Pinned so the sending domain matches and only W002 is under test.
    settings.SITE_URL = "https://example.com"
    settings.DEFAULT_FROM_EMAIL = "ops@example.com"
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", "ops@example.com")

    warnings = email_delivery_configured(None)
    assert [w.id for w in warnings] == ["core.W002"]
    assert "EMAIL_HOST_USER" in warnings[0].msg


def test_unset_from_address_is_flagged(settings, monkeypatch):
    """The commonest cause of silent non-delivery: an unverified sender."""
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST = "smtp-relay.brevo.com"
    settings.EMAIL_HOST_USER = "user"
    settings.EMAIL_HOST_PASSWORD = "secret"
    monkeypatch.delenv("DEFAULT_FROM_EMAIL", raising=False)

    ids = [w.id for w in email_delivery_configured(None)]
    assert "core.W003" in ids


def test_a_foreign_sending_domain_is_flagged(settings, monkeypatch):
    """Accepted by the relay, then silently dropped — the hardest one to see."""
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST = "smtp-relay.brevo.com"
    settings.EMAIL_HOST_USER = "user"
    settings.EMAIL_HOST_PASSWORD = "secret"
    settings.SITE_URL = "https://mycooperativeos.com"
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", "no-reply@cooperativeos.africa")
    settings.DEFAULT_FROM_EMAIL = "CooperativeOS <no-reply@cooperativeos.africa>"

    warnings = email_delivery_configured(None)
    assert [w.id for w in warnings] == ["core.W004"]
    assert "cooperativeos.africa" in warnings[0].msg


@pytest.mark.parametrize(
    "from_email",
    [
        "no-reply@mycooperativeos.com",          # exact match
        "no-reply@mail.mycooperativeos.com",     # sending subdomain
        "CooperativeOS <ops@mycooperativeos.com>",  # display name included
    ],
)
def test_a_matching_sending_domain_is_accepted(settings, monkeypatch, from_email):
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST = "smtp-relay.brevo.com"
    settings.EMAIL_HOST_USER = "user"
    settings.EMAIL_HOST_PASSWORD = "secret"
    settings.SITE_URL = "https://www.mycooperativeos.com"
    settings.DEFAULT_FROM_EMAIL = from_email
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", from_email)

    assert email_delivery_configured(None) == []


def test_a_fully_configured_relay_is_silent(settings, monkeypatch):
    """Everything set and the sending domain matching: nothing to report."""
    from core.checks import email_delivery_configured

    settings.EMAIL_BACKEND = SMTP
    settings.EMAIL_HOST = "smtp-relay.brevo.com"
    settings.EMAIL_HOST_USER = "user"
    settings.EMAIL_HOST_PASSWORD = "secret"
    settings.SITE_URL = "https://example.com"
    settings.DEFAULT_FROM_EMAIL = "CooperativeOS <ops@example.com>"
    monkeypatch.setenv("DEFAULT_FROM_EMAIL", "CooperativeOS <ops@example.com>")

    assert email_delivery_configured(None) == []


# ── Cache headers on the SPA build ──────────────────────────────────────────
# Unit-tested directly rather than through a request: WhiteNoise's middleware
# is only installed under the prod settings, but the hook is a plain function
# and the contract worth pinning is what it puts in Cache-Control.
@pytest.mark.parametrize(
    "url", ["/sw.js", "/registerSW.js", "/manifest.webmanifest", "/index.html"],
)
def test_files_that_choose_the_build_are_never_cached(url):
    """A cached service worker keeps serving the previous bundle.

    That is the failure where a deploy appears to do nothing: the shell is
    no-cache and the assets are hashed, but the worker handing them out is
    itself stale, so the browser never learns there is a new build.
    """
    from core.static_headers import add_spa_cache_headers

    headers: dict[str, str] = {}
    add_spa_cache_headers(headers, f"/srv/dist{url}", url)
    assert "no-cache" in headers["Cache-Control"]
    assert "max-age=3600" not in headers["Cache-Control"]


def test_hashed_bundles_are_cached_forever():
    """Their names change with their contents, so staleness is impossible."""
    from core.static_headers import add_spa_cache_headers

    headers: dict[str, str] = {}
    url = "/assets/index-DXh8oOmz.js"
    add_spa_cache_headers(headers, f"/srv/dist{url}", url)
    assert "immutable" in headers["Cache-Control"]


def test_unrecognised_files_keep_whitenoise_s_own_decision():
    """The hook narrows two cases; it must not take over the rest."""
    from core.static_headers import add_spa_cache_headers

    headers = {"Cache-Control": "max-age=3600, public"}
    add_spa_cache_headers(headers, "/srv/dist/favicon.svg", "/favicon.svg")
    assert headers["Cache-Control"] == "max-age=3600, public"


@pytest.mark.parametrize("path", ["/api/v1/", "/api/v1/members/", "/admin/"])
def test_catch_all_does_not_shadow_server_routes(client, path):
    """Anything server-side must never be answered with the SPA shell."""
    response = client.get(path)
    assert SPA_MARKER not in response.content


@pytest.mark.parametrize("path", ["/admin", "/api/v1", "/static"])
def test_bare_prefixes_are_not_swallowed_by_the_spa(client, path):
    """"/admin" without a trailing slash is what people actually type.

    The catch-all must leave it to Django's APPEND_SLASH redirect rather than
    answering with the SPA shell.
    """
    response = client.get(path)
    assert SPA_MARKER not in response.content, (
        f"{path} was answered with the SPA shell instead of reaching Django"
    )


def test_spa_route_resembling_a_reserved_prefix_still_works(client):
    """A client route merely starting with a reserved word must still resolve."""
    response = client.get("/administrators")
    assert response.status_code == 200
    assert SPA_MARKER in response.content


def test_unknown_api_path_still_returns_404_not_the_shell(client):
    """A typo'd endpoint must fail loudly, not silently return HTML."""
    response = client.get("/api/v1/definitely-not-an-endpoint/")
    assert response.status_code == 404
    assert SPA_MARKER not in response.content


def test_admin_login_is_reachable_and_not_the_shell(client):
    response = client.get(reverse("admin:login"))
    assert response.status_code == 200
    assert b"coop-logo" in response.content      # the themed admin, not the SPA
    assert SPA_MARKER not in response.content


@pytest.fixture
def media_probe():
    """Write a probe file into the real MEDIA_ROOT, then clean it up.

    It has to be the real MEDIA_ROOT: config.urls binds ``document_root`` at
    import time, so overriding the setting afterwards would not reach the view.
    """
    root = Path(settings.MEDIA_ROOT) / "deploy_probe"
    root.mkdir(parents=True, exist_ok=True)
    (root / "probe.txt").write_text("hello")
    try:
        yield "deploy_probe/probe.txt"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_media_is_served_when_debug_is_off(client, media_probe):
    """Uploaded files must stay reachable in production, where DEBUG is False."""
    assert settings.DEBUG is False, "tests should run with DEBUG off"
    response = client.get(f"/media/{media_probe}")
    assert response.status_code == 200
    body = b"".join(response.streaming_content) if response.streaming \
        else response.content
    assert b"hello" in body
