"""
Commands that must not be pointed at the live database by accident.

Seen in production: ``seed_platform`` was run on the live server. It creates
fictional cooperatives, invoices, support tickets and users; it only stopped
because the first fake society's name hit a character-set error.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tenants.models import Cooperative

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("command", ["seed_platform", "seed_demo"])
def test_demo_seeders_refuse_production(settings, command):
    settings.DEMO_SEEDING_ALLOWED = False
    before = Cooperative.objects.count()

    with pytest.raises(CommandError, match="fictional"):
        call_command(command)

    assert Cooperative.objects.count() == before


def test_charset_fix_explains_itself_off_mysql():
    """Tests run on SQLite; on anything but MySQL there is nothing to do."""
    with pytest.raises(CommandError, match="not MySQL"):
        call_command("fix_db_charset")
