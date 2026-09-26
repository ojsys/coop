"""Give existing cooperatives an officer, where they have none.

Cooperatives created before the application flow made the applicant a member
have no members at all — nobody to notify at go-live, nobody who can sign in,
and nobody who can admit anyone else. New applications no longer land in that
state; this repairs the ones that already did.

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        python manage.py backfill_founding_admins --dry-run

Run the dry run first: it lists what would change and, importantly, which
cooperatives cannot be repaired automatically because nothing on record says
who runs them.

Safe to re-run — creating a founding admin is idempotent, and a cooperative
that already has a privileged officer is skipped.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from accounts.join_services import create_founding_admin
from accounts.models import Membership
from tenants.models import Cooperative

# The application flow writes "Contact: Name <email> phone" into the onboarding
# notes, so the applicant's real name is usually recoverable.
CONTACT_LINE = re.compile(r"Contact:\s*(?P<name>[^<]+?)\s*<(?P<email>[^>]+)>")


def _applicant_name(cooperative, fallback_email: str) -> str:
    """Their real name from the onboarding notes, else something readable."""
    item = cooperative.onboarding_items.first()
    if item and item.notes:
        match = CONTACT_LINE.search(item.notes)
        if match:
            return match.group("name").strip()
    # Better than blank on a statement, and they can correct it in their
    # profile once they have set a password.
    local = fallback_email.split("@")[0]
    return local.replace(".", " ").replace("_", " ").title()


class Command(BaseCommand):
    help = "Create a founding officer for cooperatives that have none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.")
        parser.add_argument(
            "--cooperative", metavar="SLUG",
            help="Limit to one cooperative.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        coops = Cooperative.objects.order_by("id")
        if options["cooperative"]:
            coops = coops.filter(slug=options["cooperative"])
            if not coops.exists():
                self.stdout.write(self.style.ERROR(
                    f"No cooperative with slug {options['cooperative']!r}."))
                return

        repaired, skipped, stuck = [], [], []

        for coop in coops:
            members = list(
                Membership.all_objects.filter(cooperative=coop)
                .select_related("role"))
            if any(m.role and m.role.is_privileged for m in members):
                skipped.append(coop)
                continue

            if not coop.contact_email:
                stuck.append(coop)
                continue

            name = _applicant_name(coop, coop.contact_email)
            if dry_run:
                self.stdout.write(
                    f"  would add {name} <{coop.contact_email}> "
                    f"as officer of {coop.name}")
            else:
                membership = create_founding_admin(
                    coop, full_name=name, email=coop.contact_email,
                    phone=coop.contact_phone or "")
                self.stdout.write(
                    f"  {coop.name}: {membership.member_no} "
                    f"{membership.user.email} ({membership.role.name})")
            repaired.append(coop)

        self.stdout.write("")
        verb = "would repair" if dry_run else "repaired"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {len(repaired)} · already had an officer {len(skipped)} "
            f"· cannot repair {len(stuck)}"))

        if stuck:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "No contact address on record, so there is nobody to make an "
                "officer. Set one on the cooperative (Django admin → "
                "Cooperatives), or add a member through the console, then "
                "re-run:"))
            for coop in stuck:
                self.stdout.write(f"  - {coop.slug} ({coop.name})")

        if not dry_run and repaired:
            self.stdout.write("")
            self.stdout.write(
                "Each new officer was emailed a set-password link. They will "
                "not be able to sign in until they use it.")
