"""
Thread-local storage for the *active tenant* (cooperative).

The whole platform is multi-tenant on a shared schema. Rather than thread a
``cooperative_id`` through every call site, we bind the current cooperative to
a thread-local at the edge of the request (see ``core.middleware``) and let the
tenant-scoped model manager read it back transparently.

For code that runs outside a request (management commands, Celery tasks, tests)
use :func:`use_tenant` as a context manager:

    with use_tenant(coop):
        Membership.objects.create(...)
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_state = threading.local()


def get_current_cooperative():
    """Return the cooperative bound to this thread, or ``None``."""
    return getattr(_state, "cooperative", None)


def set_current_cooperative(cooperative) -> None:
    """Bind ``cooperative`` (or ``None``) as the active tenant for this thread."""
    _state.cooperative = cooperative


@contextmanager
def use_tenant(cooperative):
    """Temporarily bind ``cooperative`` as the active tenant, restoring after."""
    previous = get_current_cooperative()
    set_current_cooperative(cooperative)
    try:
        yield cooperative
    finally:
        set_current_cooperative(previous)
