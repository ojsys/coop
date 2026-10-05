"""
Counting app downloads, and serving the APK that gets counted.

The marketing page used to link straight at the uploaded file, which cannot be
measured. It now links to an endpoint that records the download and redirects —
so the browser still fetches from the same place and a large APK never passes
through a Django worker.

Two deliberate choices are pinned here:

* **A daily tally, not a row per download.** The useful question is whether
  adoption is moving, and a trend answers that. Keeping counts also means the
  table holds no IP address, user agent or member — nothing personal — so it
  needs no retention policy.
* **Counting never blocks the download.** If the tally write fails, the member
  still gets the app.
"""
from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from platform_admin.models import AppDownloadTally, SiteContent

pytestmark = pytest.mark.django_db


def _url(platform="android"):
    return reverse("public-app-download", kwargs={"platform": platform})


@pytest.fixture
def site():
    return SiteContent.objects.first() or SiteContent.objects.create()


# ── Serving ─────────────────────────────────────────────────────────────────
def test_it_redirects_to_the_uploaded_apk(client, site):
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    resp = client.get(_url())

    assert resp.status_code == 302
    assert "CooperativeOS.apk" in resp["Location"]


def test_a_store_listing_wins_over_a_side_loaded_file(client, site):
    """Once it is on Play, members should land there — a side-loaded APK never
    updates itself."""
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.android_store_url = "https://play.google.com/store/apps/details?id=x"
    site.save(update_fields=["android_apk", "android_store_url"])

    resp = client.get(_url())

    assert resp["Location"] == site.android_store_url


def test_nothing_published_is_a_404_not_a_broken_redirect(client, site):
    site.android_apk = None
    site.android_store_url = ""
    site.save(update_fields=["android_apk", "android_store_url"])

    assert client.get(_url()).status_code == 404


def test_an_unknown_platform_is_a_404(client, site):
    assert client.get(_url("windows")).status_code == 404


def test_it_needs_no_login(client, site):
    """Anyone on the marketing site must be able to download it."""
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    resp = client.get(_url())

    assert resp.status_code == 302


# ── Counting ────────────────────────────────────────────────────────────────
def test_a_download_is_counted(client, site):
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    client.get(_url())

    tally = AppDownloadTally.objects.get()
    assert tally.platform == "android"
    assert tally.date == timezone.localdate()
    assert tally.count == 1


def test_repeat_downloads_accumulate_on_one_row(client, site):
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    for _ in range(4):
        client.get(_url())

    assert AppDownloadTally.objects.count() == 1, "one row per platform per day"
    assert AppDownloadTally.objects.get().count == 4


def test_platforms_are_counted_separately(client, site):
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.ios_store_url = "https://apps.apple.com/app/id1"
    site.save(update_fields=["android_apk", "ios_store_url"])

    client.get(_url("android"))
    client.get(_url("ios"))
    client.get(_url("ios"))

    assert AppDownloadTally.total("android") == 1
    assert AppDownloadTally.total("ios") == 2
    assert AppDownloadTally.total() == 3


def test_a_failed_download_is_not_counted(client, site):
    """Nothing to serve means nothing to count."""
    site.android_apk = None
    site.android_store_url = ""
    site.save(update_fields=["android_apk", "android_store_url"])

    client.get(_url())

    assert AppDownloadTally.objects.count() == 0


def test_a_broken_counter_still_serves_the_download(client, site, monkeypatch):
    """A counter must never stand between a member and the app."""
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    def _boom(platform):
        raise RuntimeError("tally table is on fire")

    monkeypatch.setattr(AppDownloadTally, "record", staticmethod(_boom))

    resp = client.get(_url())

    assert resp.status_code == 302
    assert "CooperativeOS.apk" in resp["Location"]


def test_the_total_is_the_sum_of_the_rows(site):
    """Not a separate counter — there is nothing that can drift out of step."""
    import datetime

    today = timezone.localdate()
    AppDownloadTally.objects.create(platform="android", date=today, count=5)
    AppDownloadTally.objects.create(
        platform="android", date=today - datetime.timedelta(days=1), count=7)

    assert AppDownloadTally.total("android") == 12


def test_the_tally_holds_no_personal_data(client, site):
    """By construction: a date, a platform and a count. Nothing to leak."""
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    client.get(_url(), HTTP_USER_AGENT="Mozilla/5.0 (very identifying)",
               REMOTE_ADDR="41.58.1.1")

    fields = {f.name for f in AppDownloadTally._meta.fields}
    assert fields == {"id", "platform", "date", "count"}


# ── The marketing page points at the counter ────────────────────────────────
def test_the_site_content_links_through_the_counter(client, site):
    """Otherwise the link bypasses counting entirely."""
    site.android_apk = "site_apps/CooperativeOS.apk"
    site.save(update_fields=["android_apk"])

    apps = client.get("/api/v1/public/site-content/").json()["apps"]

    assert apps["android_url"].endswith("/public/app/android/")
    assert "CooperativeOS.apk" not in apps["android_url"]


def test_no_android_link_when_there_is_nothing_to_download(client, site):
    site.android_apk = None
    site.android_store_url = ""
    site.save(update_fields=["android_apk", "android_store_url"])

    apps = client.get("/api/v1/public/site-content/").json()["apps"]

    assert apps["android_url"] is None
