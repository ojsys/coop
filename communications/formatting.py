"""
Plain text an officer or the platform team types, as email-safe HTML.

One small set of marks, the same as the legal pages, so people write one way
everywhere: a blank line starts a paragraph, ``- `` a bullet, ``**bold**``,
and ``[text](https://…)`` a link. Everything is escaped first, so nothing
typed is ever interpreted as HTML.
"""
from __future__ import annotations

import re
from html import escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_P = "margin:0 0 14px;"


def _inline(line: str) -> str:
    """Escape first, then add the two marks — so nothing typed is ever HTML."""
    html = escape(line, quote=False)
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    return _LINK.sub(
        lambda m: f'<a href="{escape(m.group(2))}" '
                  f'style="color:#0b4f3a;">{m.group(1)}</a>', html)


def render_body(text: str) -> str:
    """Paragraphs, bullets, bold and links; everything else is plain text."""
    blocks = re.split(r"\n\s*\n", text.strip())
    parts = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if lines and all(ln.lstrip().startswith("- ") for ln in lines):
            items = "".join(f'<li style="margin:0 0 6px;">'
                            f'{_inline(ln.lstrip()[2:])}</li>'
                            for ln in lines)
            parts.append(f'<ul style="{_P}padding-left:20px;">{items}</ul>')
        elif lines:
            parts.append(f'<p style="{_P}">'
                         + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return "".join(parts)
