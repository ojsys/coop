"""
List the societies the platform already knew in the member signup search.

The search used to show verified societies only. That was right for a society
that signed itself up, but it also hid the ones from before self-serve signup
that 0010 switched on with their KYC not started — societies the team had
already spoken to, whose members then could not find them to join.

Every existing society is listed except those that signed themselves up
(audited as ``cooperative.signup``): those stay out of the search until their
KYC is verified, exactly as a new signup does.
"""
from django.db import migrations, models


def forwards(apps, schema_editor):
    Cooperative = apps.get_model("tenants", "Cooperative")
    AuditLog = apps.get_model("audit", "AuditLog")

    self_serve = (AuditLog._base_manager.filter(action="cooperative.signup")
                  .values_list("cooperative_id", flat=True))
    (Cooperative._base_manager.exclude(pk__in=self_serve)
     .update(publicly_listed=True))


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0011_member_cap_follows_plan'),
        ('audit', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='cooperative',
            name='publicly_listed',
            field=models.BooleanField(default=False, help_text='Show in the member signup search before KYC is verified.'),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
