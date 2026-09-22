"""
API error logging.

DRF converts exceptions into responses *before* Django's own error handling
runs, so a 400 never reaches the ``django.request`` logger and is invisible in
any log — which is precisely the case you hit when an upload is rejected and
the client only shows a generic message.

This handler records every API error with enough context to identify it (method,
path, status, user, and the response body DRF is about to return), then hands
off to DRF's default behaviour unchanged. It alters no response.
"""
from __future__ import annotations

import logging

from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("api.errors")


def logging_exception_handler(exc, context):
    """Log the failure, then return exactly what DRF would have returned."""
    response = drf_exception_handler(exc, context)

    request = context.get("request")
    view = context.get("view")
    method = getattr(request, "method", "?")
    path = getattr(request, "path", "?")
    user = getattr(request, "user", None)
    who = getattr(user, "email", None) or "anonymous"
    view_name = type(view).__name__ if view is not None else "?"

    if response is None:
        # Not a DRF exception: it becomes a 500 and Django logs the traceback
        # separately. Record the context so the two can be correlated.
        logger.exception(
            "Unhandled %s in %s %s (user=%s, view=%s)",
            type(exc).__name__, method, path, who, view_name,
        )
        return None

    # 5xx is ours to fix; 4xx is usually the caller's, so keep them separable.
    log = logger.error if response.status_code >= 500 else logger.warning
    log(
        "%s %s -> %s (user=%s, view=%s): %s",
        method, path, response.status_code, who, view_name,
        getattr(response, "data", None),
    )
    return response
