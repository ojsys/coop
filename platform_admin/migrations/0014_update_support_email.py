"""Move the platform support address onto the deployment's own domain.

Changing a model's default does nothing to rows that already exist. The
profile row was created back in 0004 with ``support@cooperativeos.africa``
baked in, and that value — not the new default — is what the public site's
footer and every "Talk to us" link render. Without this, the address stays
wrong on a live site no matter how many times the code is redeployed.

Guarded to the old default: an address someone has deliberately set in the
admin is left exactly as they set it.
"""
from django.db import migrations

OLD = "support@cooperativeos.africa"
NEW = "info@mycooperativeos.com"


def move_support_address(apps, schema_editor):
    PlatformProfile = apps.get_model("platform_admin", "PlatformProfile")
    PlatformProfile.objects.filter(support_email=OLD).update(support_email=NEW)


def restore_support_address(apps, schema_editor):
    PlatformProfile = apps.get_model("platform_admin", "PlatformProfile")
    PlatformProfile.objects.filter(support_email=NEW).update(support_email=OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0013_alter_platformprofile_support_email"),
    ]

    operations = [
        migrations.RunPython(move_support_address, restore_support_address),
    ]
