"""List phone numbers that migration 0012 would rewrite, clear or leave as typed.

Read-only. Run it on the server before ``migrate`` so nobody is surprised:

    python manage.py check_phone_identities
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from accounts.identity import try_normalize_phone
from accounts.models import Membership, User


class Command(BaseCommand):
    help = "Report phone numbers shared by accounts or not readable as a phone."

    def handle(self, *args, **options):
        regions = {}
        for user_id, country in (Membership.all_objects
                                 .exclude(cooperative__country="")
                                 .values_list("user_id", "cooperative__country")):
            regions.setdefault(user_id, country)

        holders, unreadable = defaultdict(list), []
        for user in User.objects.exclude(phone=None).exclude(phone="").order_by("pk"):
            phone = try_normalize_phone(user.phone, regions.get(user.pk))
            if phone is None:
                unreadable.append(user)
                phone = user.phone.strip()
            holders[phone].append(user)

        shared = {p: us for p, us in holders.items() if len(us) > 1}
        for phone, users in shared.items():
            self.stdout.write(f"{phone} is on {len(users)} accounts: " + ", ".join(
                f"{u.pk} {u.full_name} <{u.email}>" for u in users))
        for user in unreadable:
            self.stdout.write(f"Not a phone number, kept as typed: {user.pk} "
                              f"{user.full_name} {user.phone!r}")
        self.stdout.write(self.style.SUCCESS(
            f"{len(shared)} shared number(s), {len(unreadable)} unreadable. "
            "A shared number stays on the account that signed in most "
            "recently; the others keep signing in with their email."))
