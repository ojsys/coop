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
        # Local import: core is imported by every app, so it must not pull one
        # of them in at module load.
        from payments.models import Bank

        return Response({
            "states": states_payload(),
            "coop_types": COOP_TYPES,
            # Empty until `refresh_banks` has been run (or when no live PSP key
            # is configured). The frontend treats an empty list as "no verified
            # catalogue" and falls back to a text input, so bank entry keeps
            # working either way.
            "banks": list(
                Bank.objects.filter(active=True).values("code", "name")
            ),
            "white_label": {
                # Read straight from settings. A defensive default here would
                # be a second place for this value to live — which is exactly
                # how it drifted onto a domain the platform no longer uses.
                "cname_target": settings.WHITE_LABEL_CNAME_TARGET,
            },
        })
