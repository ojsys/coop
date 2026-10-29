"""Finish sending platform emails left pending.

The Email page sends in slices while it is open; if it was closed mid-send,
this delivers the rest. Safe to run from cron as often as you like — with
nothing pending it does nothing.
"""
from django.core.management.base import BaseCommand

from platform_admin.mailing import deliver_pending
from platform_admin.models import PlatformEmail


class Command(BaseCommand):
    help = "Deliver platform emails that are still sending."

    def handle(self, *args, **options):
        for email in PlatformEmail.objects.filter(
                status=PlatformEmail.Status.SENDING):
            remaining = deliver_pending(email, budget=600)
            self.stdout.write(f"{email.subject}: {remaining} still pending")
