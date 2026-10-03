"""Populate reconciliation from contributions that predate it recording them.

Reconciliation counts PaymentEvent rows. Until recently three settlement paths
created none — a card payment confirmed on return from the gateway, cash or a
transfer entered by an officer, and anything at all before that was fixed — so
a cooperative with a perfectly correct ledger saw an empty reconciliation
screen. New settlements now record themselves; this reconstructs the old ones.

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        python manage.py backfill_payment_events --dry-run

Reconstructed events are marked ``reconstructed: true`` in their payload and
carry a note saying so. They are **not** presented as genuine provider
settlements: nothing here verifies that money arrived, it only records what the
ledger already says did. That distinction matters if anyone later audits the
reconciliation trail.

Safe to re-run. A contribution that already has any payment event against it is
skipped, and the synthetic event id is derived from the contribution, so a
second pass cannot duplicate a row.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tenants.models import Cooperative


class Command(BaseCommand):
    help = ("Create the missing payment events for already-confirmed "
            "contributions, so reconciliation reflects past activity.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.")
        parser.add_argument(
            "--cooperative", metavar="SLUG",
            help="Limit to one cooperative.")

    def handle(self, *args, **options):
        from contributions.models import Contribution
        from payments.models import PaymentEvent
        from payments.services import record_internal_settlement

        dry_run = options["dry_run"]
        coops = Cooperative.objects.all().order_by("slug")
        if options["cooperative"]:
            coops = coops.filter(slug=options["cooperative"])
            if not coops.exists():
                self.stderr.write(self.style.ERROR(
                    f"No cooperative with slug {options['cooperative']!r}."))
                return

        total_created = 0
        total_skipped = 0

        for coop in coops:
            confirmed = (
                Contribution.all_objects
                .filter(cooperative=coop,
                        status=Contribution.Status.CONFIRMED)
                .select_related("journal")
                .order_by("occurred_at", "id")
            )
            # Any event already pointing at the contribution counts — including
            # one matched by a real webhook, which must not be shadowed by a
            # reconstructed duplicate.
            already = set(
                PaymentEvent.all_objects
                .filter(cooperative=coop, matched_contribution__isnull=False)
                .values_list("matched_contribution_id", flat=True)
            )

            created = 0
            for contribution in confirmed:
                if contribution.pk in already:
                    total_skipped += 1
                    continue
                created += 1
                if not dry_run:
                    record_internal_settlement(contribution,
                                               reconstructed=True)

            total_created += created
            if created:
                verb = "would reconstruct" if dry_run else "reconstructed"
                self.stdout.write(f"  {coop.slug}: {verb} {created}")

        self.stdout.write("")
        verb = "would reconstruct" if dry_run else "reconstructed"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {total_created} settlement(s) · "
            f"already recorded {total_skipped}"))

        if total_created == 0:
            self.stdout.write(
                "Nothing to do — every confirmed contribution already has a "
                "payment event.")
        elif dry_run:
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing was written."))
        else:
            self.stdout.write(
                "These are marked as reconstructed from the ledger, not as "
                "verified provider settlements.")
