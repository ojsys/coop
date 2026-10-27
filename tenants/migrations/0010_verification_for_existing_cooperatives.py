"""Give every existing cooperative a verification record, and open the gate.

Societies already live went through the old onboarding pipeline — a person on
the platform team talked to them before switching them on — so they count as
verified; taking online payments away from them now would break working
societies for no gain.

Societies still PROSPECTIVE applied under the old flow and were waiting on that
pipeline. Signup no longer waits, so they go live now with their KYC not yet
started. Their free trial is created by ``manage.py backfill_subscriptions``,
which starts it today rather than back-dating it — run it once after this
migration is applied.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Cooperative = apps.get_model("tenants", "Cooperative")
    Verification = apps.get_model("tenants", "CooperativeVerification")

    waiting = set(Cooperative.objects.filter(status="prospective")
                  .values_list("pk", flat=True))
    have = set(Verification.objects.values_list("cooperative_id", flat=True))
    for pk in Cooperative.objects.exclude(pk__in=have).values_list(
            "pk", flat=True):
        Verification.objects.create(
            cooperative_id=pk,
            status="not_started" if pk in waiting else "verified")
    Cooperative.objects.filter(pk__in=waiting).update(status="active")


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0009_cooperativedocument_cooperativeverification"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
