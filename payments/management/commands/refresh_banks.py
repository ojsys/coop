"""
Pull the payment provider's bank catalogue into the local table.

Bank *codes* — not names — are what Paystack needs to create a cooperative's
settlement subaccount and a member's transfer recipient, so nothing can be
disbursed electronically until this table is populated.

Run it after deploying, and again whenever the provider adds a bank:

    python manage.py refresh_banks
    python manage.py refresh_banks --dry-run

It is deliberately a command rather than something fetched on demand: rendering
a settings form should never depend on an outbound HTTP call succeeding.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from payments.models import Bank, Provider


class Command(BaseCommand):
    help = "Refresh the local bank catalogue from the payment provider."

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider", default=Provider.PAYSTACK,
            choices=[Provider.PAYSTACK, Provider.FLUTTERWAVE],
            help="Which provider's catalogue to pull (default: paystack).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what the provider returns without writing anything.",
        )

    def handle(self, *args, **options):
        provider = options["provider"]

        if options["dry_run"]:
            from payments.providers import get_provider

            rows = get_provider(provider).list_banks()
            if not rows:
                self.stdout.write(self.style.WARNING(
                    "The provider returned no banks. With the dev placeholder "
                    "key that is expected — the call is skipped entirely. On a "
                    "live key it means the request failed, and bank selection "
                    "will fall back to free-text entry."
                ))
                return
            self.stdout.write(f"{len(rows)} banks returned. First five:")
            for row in rows[:5]:
                self.stdout.write(f"  {row.get('code'):>6}  {row.get('name')}")
            self.stdout.write(self.style.NOTICE(
                "Dry run — nothing written."))
            return

        summary = Bank.refresh(provider_name=provider)

        if not summary["fetched"]:
            self.stdout.write(self.style.WARNING(
                "No banks fetched; the table is unchanged. Bank selection will "
                "fall back to free-text entry until this succeeds."
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f"{summary['fetched']} banks fetched — "
            f"{summary['created']} added, {summary['updated']} updated, "
            f"{summary['deactivated']} marked inactive."
        ))
        if summary["deactivated"]:
            self.stdout.write(
                "Inactive banks are kept, not deleted: members and "
                "cooperatives may still hold their codes."
            )
