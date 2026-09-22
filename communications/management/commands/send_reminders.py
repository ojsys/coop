"""
Scheduled member reminders: contribution arrears and upcoming meetings.

There is no worker process on shared hosting, so this is the scheduled entry
point — a cPanel cron job calls it once a day:

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        /home/USER/virtualenv/coop/3.11/bin/python manage.py send_reminders

Safe to run more than once in a day: it notifies, it does not charge or post
anything. Use ``--dry-run`` to see who *would* be contacted.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from core.context import use_tenant
from tenants.models import Cooperative


class Command(BaseCommand):
    help = "Send arrears and meeting reminders to members of active societies."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be sent without sending anything.")
        parser.add_argument(
            "--cooperative", type=int, metavar="ID",
            help="Limit to one society (default: every ACTIVE society).")
        parser.add_argument("--skip-arrears", action="store_true")
        parser.add_argument("--skip-meetings", action="store_true")
        parser.add_argument(
            "--meeting-days", type=int, default=2, metavar="N",
            help="Remind about meetings starting within N days (default 2).")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        societies = Cooperative.objects.filter(status=Cooperative.Status.ACTIVE)
        if options["cooperative"]:
            societies = societies.filter(pk=options["cooperative"])

        if not societies.exists():
            self.stdout.write(self.style.WARNING("No active societies found."))
            return

        totals = {"arrears": 0, "meetings": 0}
        for society in societies:
            # Reporting reads through tenant-scoped managers, so bind the
            # tenant exactly as a request would.
            with use_tenant(society):
                if not options["skip_arrears"]:
                    totals["arrears"] += self._arrears(society, dry_run)
                if not options["skip_meetings"]:
                    totals["meetings"] += self._meetings(
                        society, options["meeting_days"], dry_run)

        prefix = "Would send" if dry_run else "Sent"
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}: {totals['arrears']} arrears reminder(s), "
            f"{totals['meetings']} meeting reminder(s) across "
            f"{societies.count()} society(ies)."
        ))

    def _arrears(self, society, dry_run):
        from reports.services import arrears_report, send_arrears_reminders

        if dry_run:
            from django.utils import timezone
            now = timezone.now()
            report = arrears_report(society, year=now.year, month=now.month)
            count = report["members_in_arrears"]
            self.stdout.write(
                f"  {society.name}: {count} member(s) in arrears")
            return count

        result = send_arrears_reminders(society)
        self.stdout.write(
            f"  {society.name}: reminded {result['reached']} member(s) "
            f"in arrears")
        return result["reached"]

    def _meetings(self, society, days_ahead, dry_run):
        from governance.reminders import send_meeting_reminders

        result = send_meeting_reminders(society, days_ahead=days_ahead,
                                        dry_run=dry_run)
        if result["meetings"]:
            self.stdout.write(
                f"  {society.name}: {result['meetings']} meeting(s) soon, "
                f"{result['reached']} notification(s)")
        return result["reached"]
