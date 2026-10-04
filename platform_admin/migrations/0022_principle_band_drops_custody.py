"""
Take custody out of the landing page's principle band.

Two rewrites have passed through this field. The original claimed we never hold
member funds in a platform wallet, which the disbursement wallet made untrue
(0019 replaced it). The replacement explained the wallet — honest, but it put an
edge case in a marketing headline: the float only exists for societies that
choose to lend electronically, and most never fund one.

This third version says what is true of every society — contributions settle to
its own bank account, and every movement sits in an append-only ledger — and
makes no claim either way about custody. That is a reduction in prominence, not
a removal of the disclosure: the float is still disclosed in the Fund wallet
flow (where an officer actually decides to deposit), in the officer Guide, and
in the privacy notice's "Money we hold" section.

Matched on a distinctive phrase from either previous version, for the same
reason 0019 was: a row carrying the first version is making a false statement,
and a row carrying the second is merely in the wrong place, so both should move.
Copy that contains neither has been edited deliberately and is left alone.
"""
from django.db import migrations

V1_PHRASE = "never hold member funds in a platform wallet"
V2_PHRASE = "disbursement float you deposit yourself"

V2_TITLE = "Ledger first. Custodian only when you ask."
V2_BODY = (
    "Collections settle through licensed payment providers straight into your "
    "cooperative's own bank account — we record, reconcile and report on them, "
    "and that money never passes through us. The only funds we ever hold are a "
    "disbursement float you deposit yourself, when you choose to lend from it: "
    "yours to see, to top up, and to withdraw whenever you want it back."
)

NEW_TITLE = "Every naira, traceable."
NEW_BODY = (
    "Contributions settle through licensed payment providers straight into your "
    "cooperative's own bank account. Every movement is recorded in an "
    "append-only double-entry ledger — nothing is edited or deleted, "
    "corrections are posted as reversals, and your auditor can follow any "
    "figure back to the entry that produced it."
)


def drop_custody_from_the_band(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    for phrase in (V1_PHRASE, V2_PHRASE):
        SiteContent.objects.filter(principle_body__contains=phrase).update(
            principle_body=NEW_BODY)
    for phrase in ("not custodian", "Custodian only when you ask"):
        SiteContent.objects.filter(principle_title__contains=phrase).update(
            principle_title=NEW_TITLE)


def restore_the_wallet_explanation(apps, schema_editor):
    """Back to the version that explained the wallet — not to the original
    claim, which was untrue."""
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteContent.objects.filter(principle_body=NEW_BODY).update(
        principle_body=V2_BODY)
    SiteContent.objects.filter(principle_title=NEW_TITLE).update(
        principle_title=V2_TITLE)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0021_alter_sitecontent_principle_body_and_more"),
    ]

    operations = [
        migrations.RunPython(drop_custody_from_the_band,
                            restore_the_wallet_explanation),
    ]
