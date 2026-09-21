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
