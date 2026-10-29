"""
Every society is findable in the member signup search, verified or not.

0012 left self-serve signups out until their KYC was verified, so every
society that signed up after the free trial launched could not be found by its
own members. KYC gates money movement only, and a join request still needs an
officer's approval, so they are all listed now; the platform can still unlist
one by hand.
"""
from django.db import migrations, models


def forwards(apps, schema_editor):
    Cooperative = apps.get_model("tenants", "Cooperative")
    Cooperative._base_manager.update(publicly_listed=True)


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0012_publicly_listed'),
    ]

    operations = [
        migrations.AlterField(
            model_name='cooperative',
            name='publicly_listed',
            field=models.BooleanField(default=True, help_text='Show in the member signup search.'),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
