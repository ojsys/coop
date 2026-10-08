"""
Unauthenticated endpoints backing the public marketing site.

The API requires authentication by default (``DEFAULT_PERMISSION_CLASSES``), so
anything the landing page needs has to opt out explicitly — and must expose only
what is safe to publish.

Note the deliberate use of a dedicated serializer rather than reusing
``platform_admin.serializers.PlanSerializer``: that one is built for the
platform console and carries operational detail (subscriber counts), which has
no business on a public page. An allowlist is the safer default here.
"""
from __future__ import annotations

from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.reference_data import (
    COOP_TYPES, countries_payload, subdivisions_payload,
)
from platform_admin.models import (
    Plan, PlatformProfile, SiteContent, SiteFeature, SiteGalleryImage,
    SiteShowcase, SiteStep, SiteTrustBadge,
)


class PublicPlanSerializer(serializers.ModelSerializer):
    """Marketing-safe view of a plan."""

    tier_display = serializers.CharField(source="get_tier_display",
                                         read_only=True)

    band_label = serializers.CharField(read_only=True)
    features = serializers.SerializerMethodField()
    extras = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = ["id", "name", "tier", "tier_display", "price_monthly",
                  "price_annual", "price_is_from", "currency", "min_members",
                  "max_members", "band_label", "description", "features",
                  "extras"]

    def get_features(self, obj) -> list:
        """The plan's gated features, labelled, in catalogue order."""
        from core.entitlements import CATALOGUE

        keys = set(obj.features or [])
        return [{"key": key, "label": label}
                for key, (label, _) in CATALOGUE.items() if key in keys]

    def get_extras(self, obj) -> list:
        return [line.strip() for line in (obj.extras or "").splitlines()
                if line.strip()]


class PublicView(APIView):
    """Base for endpoints served to anonymous visitors."""

    authentication_classes: list = []
    permission_classes = [AllowAny]


class PublicBrandingView(PublicView):
    """`GET /public/branding/` — the platform's own brand, for the public site.

    This is the platform profile, never a cooperative's: an anonymous visitor
    has no tenant context, and leaking one society's branding to another's
    visitors would be wrong.
    """

    def get(self, request):
        profile = PlatformProfile.load()
        return Response({
            "name": profile.name,
            "brand_color": profile.brand_color,
            "logo": profile.logo.url if profile.logo else None,
            "favicon": profile.favicon.url if profile.favicon else None,
            "support_email": profile.support_email,
            "support_phone": profile.support_phone,
            # Public by necessity: the browser needs it to load gtag.js, and a
            # GA4 measurement ID is not a secret — it ships in the page source
            # of every site that uses it. Empty string means "no tracking".
            "ga_measurement_id": profile.ga_measurement_id,
        })


class PublicPlansView(PublicView):
    """`GET /public/plans/` — active plans, smallest member band first.

    Served live from the database so published pricing cannot drift from what
    cooperatives are actually billed. Inactive plans are withheld: they are
    drafts or retired tiers.
    """

    def get(self, request):
        plans = (Plan.objects.filter(active=True)
                 .order_by("min_members", "price_monthly", "name"))
        return Response(PublicPlanSerializer(plans, many=True).data)


class PublicPlanCatalogueView(PublicView):
    """`GET /public/plan-catalogue/` — what every plan includes, and the
    features plans differ by. The price card renders from this, so it cannot
    promise something the app does not gate the same way."""

    def get(self, request):
        from core.entitlements import CATALOGUE, CORE

        return Response({
            "core": CORE,
            "features": [{"key": key, "label": label, "description": desc}
                         for key, (label, desc) in CATALOGUE.items()],
        })


class PublicReferenceView(PublicView):
    """`GET /public/reference/` — the catalogue the signup form needs.

    The authenticated ``/reference/`` endpoint cannot serve this: signup is a
    public page, so the request carries no token and every field fed by it
    rendered empty. (That 401 also tripped the frontend's response
    interceptor, which is what bounced visitors from /signup to /login.)

    Only genuinely public catalogue data lives here — countries and
    cooperative types. ``white_label.cname_target`` stays behind auth on
    ``/reference/``: it is deployment detail, not marketing copy.

    Subdivisions are a separate call on purpose. Nigeria alone is ~20KB of
    LGAs, and there is no reason to push that at a visitor who has not chosen
    a country yet.
    """

    def get(self, request):
        return Response({
            "countries": countries_payload(),
            "coop_types": COOP_TYPES,
        })


class PublicLegalIndexView(PublicView):
    """`GET /public/legal/` — the published policy pages, for footer links."""

    def get(self, request):
        from platform_admin.models import LegalDocument

        return Response({
            "documents": [
                {"slug": doc.slug, "title": doc.title}
                for doc in LegalDocument.objects.filter(published=True)
            ],
        })


class PublicLegalDocumentView(PublicView):
    """`GET /public/legal/<slug>/` — one policy page.

    404 for an unpublished or absent document rather than an empty page, so the
    frontend can fall back to its built-in notice. That fallback is why the
    Google Play privacy URL keeps working before any legal copy is pasted in.
    """

    def get(self, request, slug):
        from platform_admin.models import LegalDocument

        doc = LegalDocument.objects.filter(slug=slug, published=True).first()
        if doc is None:
            return Response({"detail": "No published document at that slug."},
                            status=404)
        return Response({
            "slug": doc.slug,
            "title": doc.title,
            "body": doc.body,
            "effective_date": doc.effective_date,
            "updated_at": doc.updated_at,
        })


class PublicSiteContentView(PublicView):
    """`GET /public/site-content/` — the editable copy and imagery.

    Built by hand rather than with a ModelSerializer so image fields become
    URLs (or ``null``) instead of raw paths, and so the hidden rows are
    filtered out here rather than trusted to the client.
    """

    def get(self, request):
        # Deliberately not SiteContent.load(): that is a get_or_create, and a
        # GET from an anonymous visitor must not write a row. An unsaved
        # instance carries exactly the same field defaults, so the page still
        # renders before the seeding migration has ever run.
        content = SiteContent.objects.first() or SiteContent()

        def url(field):
            return field.url if field else None

        return Response({
            "hero": {
                "eyebrow": content.hero_eyebrow,
                "title": content.hero_title,
                "subtitle": content.hero_subtitle,
                "primary_cta": content.hero_primary_cta,
                "secondary_cta": content.hero_secondary_cta,
                "image": url(content.hero_image),
                "image_alt": content.hero_image_alt,
            },
            "principle": {
                "eyebrow": content.principle_eyebrow,
                "title": content.principle_title,
                "body": content.principle_body,
            },
            "features_title": content.features_title,
            "features_intro": content.features_intro,
            "features": [
                {"icon": f.icon, "title": f.title, "body": f.body}
                for f in SiteFeature.objects.filter(visible=True)
            ],
            "showcase_title": content.showcase_title,
            "showcase_intro": content.showcase_intro,
            "showcase": [
                {"title": s.title, "body": s.body,
                 "bullets": s.bullet_list(),
                 "image": url(s.image), "image_alt": s.image_alt}
                for s in SiteShowcase.objects.filter(visible=True)
            ],
            "steps_title": content.steps_title,
            "steps_intro": content.steps_intro,
            "steps": [
                {"number": s.number, "icon": s.icon, "title": s.title,
                 "body": s.body}
                for s in SiteStep.objects.filter(visible=True)
            ],
            "apps": {
                "title": content.apps_title,
                "body": content.apps_body,
                "note": content.apps_note,
                # Both point at the counting endpoint rather than at the file
                # or the store, so a download can be measured. It redirects to
                # whichever destination is configured — a published store
                # listing still wins over a side-loaded APK, so uploading one
                # does not strand members on the raw file.
                "android_url": (
                    request.build_absolute_uri(
                        reverse("public-app-download",
                                kwargs={"platform": "android"}))
                    if (content.android_store_url or content.android_apk)
                    else None),
                "ios_url": (
                    request.build_absolute_uri(
                        reverse("public-app-download",
                                kwargs={"platform": "ios"}))
                    if content.ios_store_url else None),
                "screenshot": url(content.app_screenshot),
                "screenshot_alt": content.app_screenshot_alt,
            },
            "cta": {
                "title": content.cta_title,
                "body": content.cta_body,
                "button": content.cta_button,
                "image": url(content.cta_image),
                "image_alt": content.cta_image_alt,
            },
            "gallery_title": content.gallery_title,
            "gallery_intro": content.gallery_intro,
            "gallery": [
                {"image": url(g.image), "alt_text": g.alt_text,
                 "caption": g.caption}
                for g in SiteGalleryImage.objects.filter(visible=True)
                if g.image
            ],
            # The list above carries visible rows only. Without this the
            # frontend cannot tell "no photos uploaded" (show the bundled
            # defaults) from "every photo hidden" (show nothing) — and an
            # admin who hid them all would get the defaults back instead.
            "gallery_has_custom": SiteGalleryImage.objects.exists(),
            "trust_badges": [
                {"icon": b.icon, "text": b.text}
                for b in SiteTrustBadge.objects.filter(visible=True)
            ],
            "pricing": {
                "title": content.pricing_title,
                "intro": content.pricing_intro,
                "fallback": content.pricing_fallback,
            },
            "footer": {
                "tagline": content.footer_tagline,
                "disclaimer": content.footer_disclaimer,
            },
            "meta": {
                "title": content.meta_title,
                "description": content.meta_description,
            },
        })


class PublicSubdivisionsView(PublicView):
    """`GET /public/reference/subdivisions/<code>/` — one country's regions.

    Returns ``[]`` for a country we ship no verified data for, which the
    frontend reads as "fall back to free text" rather than as an error. An
    unrecognised code is therefore a 200 with an empty list, not a 404: the
    form should degrade, not break, on a country we simply have not mapped.
    """

    def get(self, request, code: str):
        return Response({
            "country": code.upper(),
            "regions": subdivisions_payload(code),
        })


class PublicAppDownloadView(APIView):
    """`GET /public/app/<platform>/` — count a download, then hand it over.

    The marketing page links here instead of straight at the APK, because a link
    to a file in media cannot be measured. The redirect means the browser still
    ends up downloading from the same place, so nothing is proxied through Django
    and a large APK does not tie up a worker.

    A store URL takes precedence over an uploaded APK: once the app is on Google
    Play, members should land there for updates rather than side-loading a file
    that will never update itself.

    Counting is best-effort by design. If the tally write fails the download
    still proceeds — a broken counter must never stop someone getting the app.
    """

    permission_classes = [AllowAny]

    DESTINATIONS = {
        "android": ("android_store_url", "android_apk"),
        "ios": ("ios_store_url", None),
    }

    def get(self, request, platform: str):
        from platform_admin.models import AppDownloadTally, SiteContent

        if platform not in self.DESTINATIONS:
            raise Http404("Unknown platform.")

        content = SiteContent.objects.first()
        if content is None:
            raise Http404("No app has been published yet.")

        store_field, file_field = self.DESTINATIONS[platform]
        target = getattr(content, store_field, "") or ""
        if not target and file_field:
            uploaded = getattr(content, file_field, None)
            target = uploaded.url if uploaded else ""
        if not target:
            raise Http404("No download is available for this platform.")

        try:
            AppDownloadTally.record(platform)
        except Exception:                            # noqa: BLE001
            # Never let a counter stand between a member and the app.
            import logging

            logging.getLogger("api.errors").warning(
                "Could not record an app download for %s", platform,
                exc_info=True)

        return HttpResponseRedirect(target)
