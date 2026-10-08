"""Give already-live cooperatives the subscription they never got.

Cooperatives that went live before subscription billing existed have no
``Subscription`` row, so the billing cycle cannot see them: they are never
invoiced, never reminded, and never counted in MRR. This creates the missing
trial subscriptions so they join the normal cycle.

    cd ~/coop && DJANGO_SETTINGS_MODULE=config.settings.prod \\
        python manage.py backfill_subscriptions --dry-run

**The free month starts today, not at the original go-live date.** Backdating
would make a tenant due immediately — and anyone live longer than
BILLING_SUSPEND_AFTER_DAYS instantly suspendable — for a subscription nobody
has ever mentioned to them. Every backfilled cooperative gets the same month of
notice a new one gets.

Only ACTIVE cooperatives are touched: prospective ones have not gone live, and
suspended or closed ones should not have a trial started on their behalf.

Safe to re-run — a cooperative that already has a subscription is skipped. Run
the dry run first: it lists which cooperatives cannot be given one because no
active plan matches their tier, and those will otherwise operate free and
silently for ever.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from platform_admin.billing import active_members, ensure_subscription, plan_for
from platform_admin.models import Subscription
from tenants.models import Cooperative


class Command(BaseCommand):
    help = "Create trial subscriptions for live cooperatives that have none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change without writing anything.")
        parser.add_argument(
            "--cooperative", metavar="SLUG",
            help="Limit to one cooperative.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        coops = Cooperative.objects.filter(status=Cooperative.Status.ACTIVE)
        if options["cooperative"]:
            coops = coops.filter(slug=options["cooperative"])
            if not coops.exists():
                self.stderr.write(self.style.ERROR(
                    f"No ACTIVE cooperative with slug "
                    f"{options['cooperative']!r}."))
                return

        already = 0
        created = []
        no_plan = []

        for coop in coops.order_by("slug"):
            if Subscription.objects.filter(cooperative=coop).exists():
                already += 1
                continue
            if plan_for(coop) is None:
                no_plan.append(coop)
                continue
            created.append(coop)
            plan = plan_for(coop)
            if dry_run:
                self.stdout.write(
                    f"  would start {coop.slug} on {plan.name}")
            else:
                ensure_subscription(coop)
                self.stdout.write(f"  {coop.slug} → {plan.name}")

        self.stdout.write("")
        verb = "would start" if dry_run else "started"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} a free month for {len(created)} · "
            f"already subscribed {already} · "
            f"no matching plan {len(no_plan)}"))

        if no_plan:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "No active plan's member band holds these cooperatives, so "
                "they cannot be billed. Create a plan for their band and "
                "re-run — until then they operate free and nothing says so:"))
            for coop in no_plan:
                self.stdout.write(
                    f"  - {coop.slug} ({active_members(coop):,} members)")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run — nothing was written."))
        elif created:
            self.stdout.write("")
            self.stdout.write(
                f"Their first invoice falls due in "
                f"{settings.BILLING_TRIAL_DAYS} days — they are not billed "
                f"for the time they were live before this.")
