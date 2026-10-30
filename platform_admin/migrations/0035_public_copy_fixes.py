"""Correct public wording that contradicted itself.

- "How it works" said six to ten weeks while the call to action said two
  minutes. Both are true of different things: a society is live the moment it
  signs up, and only an assisted move of its old records takes weeks.
- The mobile-apps note said the apps were "on the way" beside a working
  Android download. The page now labels that download as a direct one, and the
  note tells iPhone users to use the web app instead.
- The Terms gave a different contact address from the one the site shows.
  They now give the address set in Platform Settings.

Each change applies only to the old wording, so anything already edited in the
admin or on the Legal pages screen is left as it is.
"""
from django.db import migrations, models

OLD_STEPS = ("From your first conversation with us to members checking their "
             "own statements — usually six to ten weeks.")
NEW_STEPS = ("You're live the day you sign up. If we're moving years of "
             "records across for you, that part usually takes six to ten "
             "weeks — and you use the platform while it happens.")
OLD_NOTE = ("Android and iOS apps are on the way — download links will "
            "appear here.")
NEW_NOTE = ("On iPhone? Use the web app: open this site in Safari, tap Share, "
            "then Add to Home Screen.")
OLD_EMAIL = "info@mycooperativeos.com"


def forwards(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteContent.objects.filter(steps_intro=OLD_STEPS).update(
        steps_intro=NEW_STEPS)
    SiteContent.objects.filter(apps_note=OLD_NOTE).update(apps_note=NEW_NOTE)

    PlatformProfile = apps.get_model("platform_admin", "PlatformProfile")
    LegalDocument = apps.get_model("platform_admin", "LegalDocument")
    profile = PlatformProfile.objects.first()
    email = profile.support_email if profile else ""
    if not email or email == OLD_EMAIL:
        return
    for doc in LegalDocument.objects.filter(body__contains=OLD_EMAIL):
        doc.body = doc.body.replace(OLD_EMAIL, email)
        doc.save(update_fields=["body"])


def backwards(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteContent.objects.filter(steps_intro=NEW_STEPS).update(
        steps_intro=OLD_STEPS)
    SiteContent.objects.filter(apps_note=NEW_NOTE).update(apps_note=OLD_NOTE)


class Migration(migrations.Migration):
    dependencies = [("platform_admin", "0034_terms_and_conditions")]
    operations = [
        migrations.AlterField(
            model_name="sitecontent", name="steps_intro",
            field=models.TextField(blank=True, default=NEW_STEPS)),
        migrations.RunPython(forwards, backwards),
    ]
