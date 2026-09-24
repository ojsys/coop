"""Emails must contain their content, and nothing of their source.

Django's ``{# ... #}`` comment is single-line only. A comment that wraps onto a
second line is not a comment at all — Django renders it verbatim. That alone
would be untidy; what made it serious is that base.html's note contained a
literal ``<style>`` tag, and an HTML parser treats everything after an unclosed
``<style>`` as stylesheet text. The entire message body was swallowed, and a
password-reset email arrived reading, in full:

    {# Email clients strip

Nothing about that failure is visible from the sending side: the message is
built, accepted by the relay and delivered. Only a test that looks at the
rendered output catches it, which is what this file is for.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core import mail

pytestmark = pytest.mark.django_db

TEMPLATE_DIR = Path(settings.BASE_DIR) / "templates"

# A `{#` with no closing `#}` later on the same line.
UNCLOSED_COMMENT = re.compile(r"\{#(?!.*#\})")


def _templates() -> list[Path]:
    return sorted(TEMPLATE_DIR.rglob("*.html"))


def test_there_are_templates_to_inspect():
    """Guard the guard: a wrong path would make every check below vacuous."""
    assert _templates(), f"no templates found under {TEMPLATE_DIR}"


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_no_comment_spans_more_than_one_line(path):
    offenders = [
        f"line {i}: {line.strip()[:66]}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if UNCLOSED_COMMENT.search(line)
    ]
    assert offenders == [], (
        f"{path.name} has a `{{# ... #}}` comment that does not close on the "
        "same line. Django renders those verbatim — use "
        "{% comment %}...{% endcomment %} instead:\n  " + "\n  ".join(offenders)
    )


# ── The rendered message ────────────────────────────────────────────────────
def _user():
    from accounts.models import User

    return User.objects.create_user(
        email="ada@example.com", full_name="Ada Okonkwo", password="x-x-x-x-x",
    )


def test_password_reset_email_carries_its_body_and_link():
    """The reported failure, pinned end to end."""
    from communications.email import send_password_reset_email

    mail.outbox.clear()
    assert send_password_reset_email(_user()) is True

    message = mail.outbox[0]
    html = message.alternatives[0][0]

    assert "Ada Okonkwo" in html, "the greeting is missing"
    assert "/reset-password?uid=" in html, "the reset link is missing"
    assert "Choose a new password" in html, "the call to action is missing"


def test_no_template_source_leaks_into_a_sent_email():
    from communications.email import send_password_reset_email

    mail.outbox.clear()
    send_password_reset_email(_user())
    message = mail.outbox[0]
    html = message.alternatives[0][0]

    for marker in ("{#", "#}", "{%", "%}"):
        assert marker not in html, f"{marker!r} leaked into the rendered email"
    assert marker_free(message.body), "template source leaked into the text part"


def marker_free(text: str) -> bool:
    return not any(m in text for m in ("{#", "#}", "{%", "%}"))


def test_nothing_opens_a_style_tag_that_is_never_closed():
    """An unclosed <style> hides every following element from the reader.

    This is the specific mechanism that emptied the password-reset email, and
    it is invisible unless you parse what was actually sent.
    """
    from communications.email import send_password_reset_email

    mail.outbox.clear()
    send_password_reset_email(_user())
    html = mail.outbox[0].alternatives[0][0]

    assert html.lower().count("<style") == html.lower().count("</style"), (
        "unbalanced <style> tags — everything after an unclosed one is treated "
        "as CSS and never displayed"
    )


def test_the_text_links_are_usable_not_html_escaped():
    """Entities must not survive into the plain-text part.

    strip_tags drops markup but leaves entities encoded, so the reset URL
    arrives as "?uid=MQ&amp;token=..." — the token is then read as
    "amp;token" and the link fails. Only readers whose client prefers text
    are affected, so it breaks quietly.
    """
    from communications.email import send_password_reset_email

    mail.outbox.clear()
    send_password_reset_email(_user())
    body = mail.outbox[0].body

    assert "&amp;" not in body, "an HTML entity leaked into the text part"
    assert "&token=" in body, "the token is not a usable query parameter"


def test_the_plain_text_alternative_is_not_empty():
    """Many members read mail on clients that prefer the text part."""
    from communications.email import send_password_reset_email

    mail.outbox.clear()
    send_password_reset_email(_user())
    body = mail.outbox[0].body

    assert "Ada Okonkwo" in body
    assert "/reset-password?uid=" in body
