"""Treat existing accounts with a password as having confirmed their email.

Until now the only way to obtain a usable password was a link emailed to the
address (the welcome or reset email), or being created by an operator. Asking
every existing officer to confirm again before they could submit KYC would be
friction with nothing learnt. Accounts still without a password — invited, link
never followed — stay unconfirmed.
"""
from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    # Django marks an unusable password with a leading "!".
    (User.objects.filter(email_verified_at__isnull=True)
     .exclude(password__startswith="!").exclude(password="")
     .update(email_verified_at=timezone.now()))


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_user_email_verified_at"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
