"""Phone or email: either may sign in, so both are unique and blank is NULL.

Existing phone numbers are rewritten in E.164 (``08034412290`` becomes
``+2348034412290``), read as Nigerian unless they carry a country code, or as
the country of the society the person belongs to. A number that will not parse
is kept as typed.

Two accounts holding the same number would block the unique constraint. Every
account made before this migration has an email, so the number stays with the
account that signed in most recently and is cleared from the others, which
keep signing in with their email. ``manage.py check_phone_identities`` lists
these before you migrate.
"""
from django.db import migrations, models


def _region_by_user(apps):
    Membership = apps.get_model("accounts", "Membership")
    regions = {}
    for user_id, country in (Membership.objects
                             .exclude(cooperative__country="")
                             .values_list("user_id", "cooperative__country")):
        regions.setdefault(user_id, country)
    return regions


def normalise(apps, schema_editor):
    from accounts.identity import try_normalize_phone

    User = apps.get_model("accounts", "User")
    User.objects.filter(email="").update(email=None)
    User.objects.filter(phone="").update(phone=None)

    regions = _region_by_user(apps)
    holders = {}
    for user in User.objects.exclude(phone=None).order_by("pk"):
        raw = user.phone.strip()
        phone = try_normalize_phone(raw, regions.get(user.pk)) or raw or None
        if phone != user.phone:
            User.objects.filter(pk=user.pk).update(phone=phone)
            user.phone = phone
        if phone:
            holders.setdefault(phone, []).append(user)

    for phone, users in holders.items():
        if len(users) < 2:
            continue
        users.sort(key=lambda u: (u.last_login is not None, u.last_login, u.pk),
                   reverse=True)
        keeper, others = users[0], users[1:]
        User.objects.filter(pk__in=[u.pk for u in others]).update(phone=None)
        print(f"\n  {phone}: kept on user {keeper.pk}, cleared from "
              f"{', '.join(str(u.pk) for u in others)}")


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_existing_passwords_confirm_email'),
        # Reads Cooperative.country to know how a local number is written.
        ('tenants', '0004_cooperative_country_alter_cooperative_lga_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='joinrequest',
            name='email',
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AlterField(
            model_name='user',
            name='email',
            field=models.EmailField(blank=True, max_length=254, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name='user',
            name='phone',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.RunPython(normalise, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='user',
            name='phone',
            field=models.CharField(blank=True, help_text='E.164, e.g. +2348034412290.', max_length=20, null=True, unique=True),
        ),
    ]
