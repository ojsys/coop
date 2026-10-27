"""
The landing page stops describing signup as an application.

Societies now sign themselves up and are live at once on a free trial, so copy
promising "we will talk through what needs migrating" before anything happens,
or that "we migrate your register" as step one, describes a process that no
longer stands in front of them.

Changing a field default does nothing to the stored SiteContent row, which is
what the page renders (see 0019). Here the rows are rewritten only where they
still hold the *old default* wording exactly: unlike 0019, the old copy is not
false — migration help is still offered — so copy an admin has edited by hand
is theirs and is left alone.
"""
from django.db import migrations, models

OLD_CTA_BODY = ("Tell us about your cooperative and we will talk through what "
                "needs migrating. Nothing is charged until you decide to go "
                "ahead.")
NEW_CTA_BODY = ("Set your cooperative up in two minutes and use it free for "
                "your first month. Want help moving your records across? "
                "We'll do that with you.")
OLD_CTA_BUTTON = "Apply as a cooperative"
NEW_CTA_BUTTON = "Start your free trial"

OLD_STEP_TITLES = ("Onboard your cooperative", "Onboard your society")
OLD_STEP_BODY = ("We migrate your existing register and contribution history, "
                 "then run in parallel with your books until the numbers "
                 "agree.")
NEW_STEP_TITLE = "Sign up in minutes"
NEW_STEP_BODY = ("Your cooperative is live the moment you sign up, free for "
                 "the first month. Verify your exco once to start taking "
                 "payments online.")


def forwards(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteStep = apps.get_model("platform_admin", "SiteStep")
    SiteContent.objects.filter(cta_body=OLD_CTA_BODY).update(
        cta_body=NEW_CTA_BODY)
    SiteContent.objects.filter(cta_button=OLD_CTA_BUTTON).update(
        cta_button=NEW_CTA_BUTTON)
    SiteStep.objects.filter(title__in=OLD_STEP_TITLES,
                            body=OLD_STEP_BODY).update(
        title=NEW_STEP_TITLE, body=NEW_STEP_BODY)


def backwards(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteStep = apps.get_model("platform_admin", "SiteStep")
    SiteContent.objects.filter(cta_body=NEW_CTA_BODY).update(
        cta_body=OLD_CTA_BODY)
    SiteContent.objects.filter(cta_button=NEW_CTA_BUTTON).update(
        cta_button=OLD_CTA_BUTTON)
    SiteStep.objects.filter(title=NEW_STEP_TITLE, body=NEW_STEP_BODY).update(
        title=OLD_STEP_TITLES[0], body=OLD_STEP_BODY)


class Migration(migrations.Migration):

    dependencies = [
        ('platform_admin', '0030_subscription_trial_reminded_days'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sitecontent',
            name='cta_body',
            field=models.TextField(default=NEW_CTA_BODY),
        ),
        migrations.AlterField(
            model_name='sitecontent',
            name='cta_button',
            field=models.CharField(default=NEW_CTA_BUTTON, max_length=40),
        ),
        migrations.RunPython(forwards, backwards),
    ]
