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

from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.reference_data import (
    COOP_TYPES, countries_payload, subdivisions_payload,
)
from platform_admin.models import (
    Plan, PlatformProfile, SiteContent, SiteFeature, SiteGalleryImage,
    SiteStep, SiteTrustBadge,
)


class PublicPlanSerializer(serializers.ModelSerializer):
    """Marketing-safe view of a plan."""

    tier_display = serializers.CharField(source="get_tier_display",
                                         read_only=True)

    class Meta:
        model = Plan
        fields = ["id", "name", "tier", "tier_display", "price_monthly",
                  "currency", "min_members", "max_members", "description"]


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
        })


class PublicPlansView(PublicView):
    """`GET /public/plans/` — active plans, cheapest first.

    Served live from the database so published pricing cannot drift from what
    cooperatives are actually billed. Inactive plans are withheld: they are
    drafts or retired tiers.
    """

    def get(self, request):
        plans = (Plan.objects.filter(active=True)
                 .order_by("price_monthly", "name"))
        return Response(PublicPlanSerializer(plans, many=True).data)


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
            "steps_title": content.steps_title,
            "steps": [
                {"number": s.number, "title": s.title, "body": s.body}
                for s in SiteStep.objects.filter(visible=True)
            ],
            "gallery_title": content.gallery_title,
            "gallery_intro": content.gallery_intro,
            "gallery": [
                {"image": url(g.image), "alt_text": g.alt_text,
                 "caption": g.caption}
                for g in SiteGalleryImage.objects.filter(visible=True)
                if g.image
            ],
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
