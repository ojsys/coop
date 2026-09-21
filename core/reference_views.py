"""Read-only reference data for the frontend (states/LGAs, coop types, etc.)."""
from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.reference_data import COOP_TYPES, states_payload


class ReferenceView(APIView):
    """`GET /api/v1/reference/` — static lookups used across the console."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "states": states_payload(),
            "coop_types": COOP_TYPES,
            "white_label": {
                "cname_target": getattr(
                    settings, "WHITE_LABEL_CNAME_TARGET",
                    "tenants.cooperativeos.africa"),
            },
        })
