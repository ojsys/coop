"""Give existing societies' Chairperson role the console access new ones get.

Only a Chairperson role still carrying exactly the old default permissions is
upgraded. A society that has edited its Chairperson role made a choice, and
this leaves it alone.
"""
from django.db import migrations

OLD = ["governance.approve", "governance.vote", "reports.view"]
NEW = ["governance.approve", "members.manage", "governance.manage",
       "comms.broadcast", "governance.vote", "reports.view"]


def _swap(apps, frm, to):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(slug="chairperson"):
        if sorted(role.permissions or []) == sorted(frm):
            role.permissions = list(to)
            role.save(update_fields=["permissions"])


def forwards(apps, schema_editor):
    _swap(apps, OLD, NEW)


def backwards(apps, schema_editor):
    _swap(apps, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_account_deletion_request"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
