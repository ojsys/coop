"""Keep the demo seeders off the live database.

``seed_demo`` and ``seed_platform`` create fictional cooperatives, members,
invoices, support tickets and users. On the live site that is not a harmless
extra: fake societies appear in the platform dashboard, fake invoices in the
revenue figures, and fake accounts can sign in. Both commands therefore refuse
to run where ``DEMO_SEEDING_ALLOWED`` is off (production) unless told
explicitly.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import CommandError

FLAG = "--i-know-this-is-production"


def add_override(parser):
    parser.add_argument(
        FLAG, action="store_true", dest="force_production",
        help="Run the demo seeder even though this is production. It creates "
             "fictional cooperatives, members, invoices and users.")


def refuse_in_production(options, command):
    if getattr(settings, "DEMO_SEEDING_ALLOWED", True):
        return
    if options.get("force_production"):
        return
    raise CommandError(
        f"{command} creates fictional cooperatives, members, invoices and "
        f"users. This is the production database, so it has not been run. "
        f"Plans and prices come from migrations ('manage.py migrate'), not "
        f"from this seeder. To seed demo data here anyway, pass {FLAG}.")
