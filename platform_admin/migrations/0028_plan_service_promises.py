"""Fill in each plan's service promises (shown on the price card).

Only where none have been written: an operator's own wording is kept. These are
commitments nothing in the code enforces — support response, training and how
much history is migrated free — so they must stay something the team can staff.
"""
from django.db import migrations

PROMISES = {
    "Starter": [
        "Email support, reply within 2 working days",
        "Built-in step-by-step guide for officers",
        "Current member register migrated free",
    ],
    "Growth": [
        "Email and WhatsApp support, next working day",
        "One live training session for officers",
        "Register plus one financial year of history migrated free",
    ],
    "Professional": [
        "A named contact, same-day support",
        "Quarterly training sessions for officers",
        "Up to three years of history migrated free",
    ],
    "Institutional": [
        "Dedicated account manager with a service-level agreement",
        "On-site training for officers",
        "Full history archive migrated",
    ],
}


def fill(apps, schema_editor):
    Plan = apps.get_model("platform_admin", "Plan")
    for name, lines in PROMISES.items():
        Plan.objects.filter(name=name, extras="").update(
            extras="\n".join(lines))


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0027_plan_features"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
