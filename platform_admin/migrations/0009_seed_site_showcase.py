"""Seed the expanded landing page.

Three things happen here:

* the feature deep-dives get their copy, so the section is not an empty band;
* the how-it-works steps get distinct icons — 0008 added the field with a
  single default, which would have drawn the same icon three times in the new
  diagram;
* the mobile-app section gets a holding note, so it is visible and obviously
  editable rather than silently hidden until someone finds the fields.

No images are seeded. Photography has to be licensed by whoever publishes it,
so the blocks fall back to a plain panel until an admin uploads their own.

Every write is guarded so re-running never overwrites an edit.
"""
from django.db import migrations

SHOWCASE = [
    (
        "Contributions that reconcile themselves",
        "Dues, levies and target savings are recorded against the member who "
        "owed them, not dropped into one pot to be untangled later. Payments "
        "settle into your society's own account and match themselves to the "
        "expected contribution.",
        [
            "Monthly dues, one-off levies and target savings in one register",
            "Payments matched to the member automatically",
            "Arrears visible the day they happen, not at year end",
            "Members read their own statement instead of calling the secretary",
        ],
    ),
    (
        "Loans, from application to final repayment",
        "An application moves through your own approval rules, and the "
        "schedule it generates is the one the ledger posts against. Nobody "
        "recalculates a balance by hand.",
        [
            "Approval requires a second officer — the maker cannot check",
            "Amortisation schedules generated, not typed",
            "Every repayment posts straight to the ledger",
            "Outstanding balance always derived, never stored and never stale",
        ],
    ),
    (
        "Books your auditor can actually follow",
        "Every figure on every report traces back to a double-entry posting. "
        "Nothing is edited after the fact: a correction is a reversal, and "
        "both sides stay on the record.",
        [
            "Append-only ledger — entries are never rewritten",
            "Corrections are reversals, with the original left intact",
            "Statements and exports built from the ledger, not a spreadsheet",
            "Full audit trail of who changed what, and when",
        ],
    ),
]

# 0008 added `icon` with one default, so all three seeded steps share it.
STEP_ICONS = {"01": "handshake", "02": "group", "03": "payments"}

APPS_NOTE = ("Android and iOS apps are on the way — download links will "
             "appear here.")


def seed(apps_registry, schema_editor):
    SiteContent = apps_registry.get_model("platform_admin", "SiteContent")
    SiteShowcase = apps_registry.get_model("platform_admin", "SiteShowcase")
    SiteStep = apps_registry.get_model("platform_admin", "SiteStep")

    if not SiteShowcase.objects.exists():
        for i, (title, body, bullets) in enumerate(SHOWCASE):
            SiteShowcase.objects.create(
                order=i, title=title, body=body, bullets="\n".join(bullets),
            )

    # Only touch steps still carrying the field default, so a choice an admin
    # has already made is never overwritten.
    for step in SiteStep.objects.filter(icon="handshake"):
        icon = STEP_ICONS.get(step.number)
        if icon and icon != step.icon:
            step.icon = icon
            step.save(update_fields=["icon"])

    content = SiteContent.objects.first()
    if content is not None and not content.apps_note:
        content.apps_note = APPS_NOTE
        content.save(update_fields=["apps_note"])


def unseed(apps_registry, schema_editor):
    """No-op: reversing 0008 drops the table and the columns anyway."""


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0008_siteshowcase_sitecontent_android_apk_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
