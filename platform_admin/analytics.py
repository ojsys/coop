"""
Normalising what an admin pastes into the Google Analytics field.

Google's setup screen hands you a whole ``<script>`` block, not a bare
measurement ID, so pasting that block is the obvious thing to do. Two ways to
handle it: store the HTML and inject it into every page, or lift the ID out and
store only that. The first means arbitrary script execution on every surface —
including the sign-in and reset-password pages — so this does the second.

Kept in its own module so both the DRF serializer and the Django admin can share
one definition of "valid", rather than each growing its own.
"""
from __future__ import annotations

import re

# GA4 ("G-"), plus legacy Universal Analytics ("UA-") so an older property is
# not rejected outright.
_ID_RE = re.compile(r"\b(G-[A-Z0-9]{4,20}|UA-\d{4,10}-\d{1,4})\b", re.IGNORECASE)
# Tag Manager containers look similar and are easy to confuse with a GA4 ID, but
# they load a different script, so they are refused with an explanation rather
# than accepted and silently ignored.
_GTM_RE = re.compile(r"\bGTM-[A-Z0-9]{4,20}\b", re.IGNORECASE)


def extract_measurement_id(value: str | None) -> str:
    """Return a bare measurement ID, or "" for blank input.

    Accepts a bare ID or the full gtag snippet. Raises ``ValueError`` with a
    message worth showing to the person who pasted it.
    """
    text = (value or "").strip()
    if not text:
        return ""

    match = _ID_RE.search(text)
    if match:
        return match.group(1).upper()

    if _GTM_RE.search(text):
        raise ValueError(
            "That is a Google Tag Manager container ID. Tag Manager is not "
            "wired up yet — paste the GA4 measurement ID instead, which looks "
            "like G-XXXXXXXXXX."
        )

    raise ValueError(
        'No measurement ID found. Paste either the ID itself (e.g. '
        '"G-XXXXXXXXXX") or the whole Google tag snippet, and the ID will be '
        'taken from it.'
    )
