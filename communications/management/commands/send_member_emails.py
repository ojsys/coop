"""Finish sending members' emails left pending.

The console's Email page sends in slices while it is open; if it was closed
mid-send, this delivers the rest. Safe to run from cron as often as you like —
with nothing pending it does nothing.
"""
from django.core.management.base import BaseCommand

from communications.member_mailing import deliver_pending
from communications.models import MemberEmail
from core.context import use_tenant


class Command(BaseCommand):
    help = "Deliver members' emails that are still sending."

    def handle(self, *args, **options):
        for email in (MemberEmail.all_objects
                      .filter(status=MemberEmail.Status.SENDING)
                      .select_related("cooperative")):
            with use_tenant(email.cooperative):
                remaining = deliver_pending(email, budget=600)
            self.stdout.write(f"{email.cooperative.name} · {email.subject}: "
                              f"{remaining} still pending")
