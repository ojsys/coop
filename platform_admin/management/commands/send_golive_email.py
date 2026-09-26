"""Send (or resend) the go-live announcement for one cooperative.

The admin has a "Resend the go-live email" action, which covers the usual
case. This exists for the times you are already on the server reading
``logs/app.log`` to find out why nothing arrived:

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        python manage.py send_golive_email imole --force

Without ``--force`` it will not mail a cooperative that has already been told,
so it is safe to run over a list without checking each one first.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from platform_admin.models import OnboardingItem
from platform_admin.services import notify_go_live
from tenants.models import Cooperative


class Command(BaseCommand):
    help = "Send or resend the go-live email for a cooperative."

    def add_arguments(self, parser):
        parser.add_argument(
            "cooperative",
            help="Cooperative slug, or numeric id.")
        parser.add_argument(
            "--force", action="store_true",
            help="Send even if it has already been sent once.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show who would be told, without sending anything.")

    def handle(self, *args, **options):
        ident = options["cooperative"]
        coop = (Cooperative.objects.filter(pk=ident).first()
                if ident.isdigit()
                else Cooperative.objects.filter(slug=ident).first())
        if coop is None:
            raise CommandError(
                f"No cooperative with slug or id {ident!r}. List them with "
                f"`manage.py shell -c \"from tenants.models import "
                f"Cooperative; print(list(Cooperative.objects.values_list("
                f"'id','slug','name')))\"`."
            )

        item = OnboardingItem.objects.filter(cooperative=coop).first()
        if item is None:
            raise CommandError(
                f"{coop.name} has no onboarding item, so there is no go-live "
                f"to announce. It was probably created directly rather than "
                f"through the onboarding pipeline."
            )

        from communications.email import cooperative_notification_emails

        recipients = cooperative_notification_emails(coop)
        self.stdout.write(f"Cooperative : {coop.name} ({coop.status})")
        self.stdout.write(f"Stage       : {item.get_stage_display()}")
        self.stdout.write(
            f"Last sent   : {item.golive_email_sent_at or 'never'}")
        self.stdout.write(
            f"Recipients  : {', '.join(recipients) if recipients else 'NONE'}")
        self.stdout.write("")

        if not recipients:
            raise CommandError(
                "Nobody to tell: this cooperative has no contact_email and no "
                "active privileged officer. Set a contact address on the "
                "cooperative, or admit an officer, then run this again."
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing sent."))
            return

        if item.golive_email_sent_at and not options["force"]:
            self.stdout.write(self.style.WARNING(
                "Already sent once. Re-run with --force to send it again."))
            return

        if notify_go_live(item, force=options["force"]):
            self.stdout.write(self.style.SUCCESS(
                f"Sent to {', '.join(recipients)}."))
            self.stdout.write(
                "Acceptance is not arrival — if it does not turn up, check the "
                "relay's activity log and `manage.py send_test_email`.")
        else:
            raise CommandError(
                "The message was not accepted. See logs/app.log for the "
                "delivery error."
            )
