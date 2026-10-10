"""Bring each society's member cap into line with its plan's member band.

The cap was a free number (5,000 by default) that nothing kept in step with the
plan, so a Starter society showed a limit of 5,000. From now on
``Subscription.sync_member_cap`` keeps it there; this fixes the rows that
already exist. An open-ended band (Institutional) leaves the cap alone.
"""
from django.db import migrations


def sync(apps, schema_editor):
    Subscription = apps.get_model("platform_admin", "Subscription")
    Cooperative = apps.get_model("tenants", "Cooperative")
    for sub in Subscription.objects.select_related("plan"):
        top = sub.plan.max_members
        if top:
            Cooperative.objects.filter(pk=sub.cooperative_id).exclude(
                member_cap=top).update(member_cap=top)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0028_plan_service_promises"),
        ("tenants", "0007_plain_size_labels"),
    ]

    operations = [
        migrations.RunPython(sync, migrations.RunPython.noop),
    ]
