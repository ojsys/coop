"""
The daily subscription billing pass.

There is no worker process on shared hosting, so this is the scheduled entry
point — a cPanel cron job calls it once a day:

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        /home/USER/virtualenv/coop/3.11/bin/python manage.py run_billing

One pass raises invoices for periods that have ended, marks anything past its
due date overdue, reminds every BILLING_REMINDER_INTERVAL_DAYS, and suspends a
cooperative only once an invoice is BILLING_SUSPEND_AFTER_DAYS past due.

Safe to run twice in a day: an invoice is not raised while one is already
outstanding, and reminders respect their own cadence. Run ``--dry-run`` first —
this one *does* send mail and *can* suspend a tenant.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tenants.models import Cooperative


class Command(BaseCommand):
    help = "Raise subscription invoices, send reminders and suspend non-payers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen without sending or changing "
                 "anything.")
        parser.add_argument(
            "--cooperative", metavar="SLUG",
            help="Limit to one cooperative (default: all of them).")

    def handle(self, *args, **options):
        from platform_admin.billing import run_billing_cycle

        dry_run = options["dry_run"]
        coop = None
        if options["cooperative"]:
            coop = Cooperative.objects.filter(
                slug=options["cooperative"]).first()
            if coop is None:
                self.stderr.write(self.style.ERROR(
                    f"No cooperative with slug {options['cooperative']!r}."))
                return

        report = run_billing_cycle(dry_run=dry_run, cooperative=coop)

        verb = "would be" if dry_run else "were"
        for key, label in (("issued", "invoiced"), ("overdue", "marked overdue"),
                           ("reminded", "reminded"), ("suspended", "SUSPENDED")):
            rows = report[key]
            if not rows:
                continue
            self.stdout.write(f"{len(rows)} {verb} {label}:")
            for row in rows:
                self.stdout.write(f"  - {row}")

        if not any(report[k] for k in ("issued", "overdue", "reminded",
                                       "suspended")):
            self.stdout.write("Nothing due. No invoices, reminders or "
                              "suspensions.")
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing was sent or changed."))
        else:
            self.stdout.write(self.style.SUCCESS("Billing pass complete."))
