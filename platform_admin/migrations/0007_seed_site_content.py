"""Seed the public site with its current wording.

The CMS tables ship populated rather than empty. An editor opening "Site
content" for the first time should see what the site actually says and change
it in place — a screen full of blank boxes gives them nothing to work from and
no idea what the original copy was.

The singleton row needs no values here: its field defaults *are* the copy, so
creating the row is enough. The ordered lists have no such mechanism, so they
are written out explicitly.

Each list is seeded only when empty, so re-running this never resurrects
something an admin deliberately deleted.
"""
from django.db import migrations

FEATURES = [
    ("group", "Members & registers",
     "Replace the paper register. Profiles, KYC documents, roles and share "
     "capital, with every change audited."),
    ("savings", "Contributions & savings",
     "Dues, levies and target savings recorded against each member, with "
     "statements they can read themselves."),
    ("request_quote", "Loans & credit",
     "Applications, approvals, amortisation schedules and repayments — each "
     "posting straight to the ledger."),
    ("how_to_vote", "Governance",
     "Meetings, attendance, resolutions and tamper-evident voting. One member "
     "one vote, or weighted by shares."),
    ("summarize", "Reports & audit",
     "Audit-ready statements and exports built from an append-only "
     "double-entry ledger, not a spreadsheet."),
    ("campaign", "Member communication",
     "Announcements and transactional notices across in-app, email and SMS, "
     "respecting each member’s choices."),
]

STEPS = [
    ("01", "Onboard your society",
     "We migrate your existing register and contribution history, then run in "
     "parallel with your books until the numbers agree."),
    ("02", "Members get access",
     "Each member signs in to a mobile-first portal to see their own "
     "contributions, savings and statements."),
    ("03", "Collections reconcile themselves",
     "Payments settle into your society’s own account and are matched to "
     "the expected contribution automatically."),
]

# The data-protection line is deliberately written to hold in every
# jurisdiction. It previously named Nigeria's NDPA 2023, which read as though
# the product were available only there.
BADGES = [
    ("verified_user",
     "Member data handled under applicable data-protection law"),
    ("lock", "Signature-verified payment webhooks"),
    ("fact_check", "Append-only ledger with full audit trail"),
    ("account_balance", "Two-factor authentication for finance roles"),
]


def seed(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteFeature = apps.get_model("platform_admin", "SiteFeature")
    SiteStep = apps.get_model("platform_admin", "SiteStep")
    SiteTrustBadge = apps.get_model("platform_admin", "SiteTrustBadge")

    # Field defaults carry the copy; creating the row is all that is needed.
    SiteContent.objects.get_or_create(pk=1)

    if not SiteFeature.objects.exists():
        for i, (icon, title, body) in enumerate(FEATURES):
            SiteFeature.objects.create(order=i, icon=icon, title=title,
                                       body=body)

    if not SiteStep.objects.exists():
        for i, (number, title, body) in enumerate(STEPS):
            SiteStep.objects.create(order=i, number=number, title=title,
                                    body=body)

    if not SiteTrustBadge.objects.exists():
        for i, (icon, text) in enumerate(BADGES):
            SiteTrustBadge.objects.create(order=i, icon=icon, text=text)


def unseed(apps, schema_editor):
    """No-op: reversing the previous migration drops these tables anyway."""


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin",
         "0006_sitecontent_sitefeature_sitegalleryimage_sitestep_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
