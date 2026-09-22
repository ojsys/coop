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

from platform_admin.models import Plan, PlatformProfile


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
