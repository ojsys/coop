"""
Retire the "we never hold member funds in a platform wallet" claim.

Changing a model's default does nothing to rows that already exist. The
SiteContent row was created long ago with the old wording baked in, and *that*
value — not the new default — is what the public landing page renders. Without
this migration the site keeps promising something the product no longer does,
however many times the code is redeployed.

The claim stopped being true when the disbursement wallet was built: a society
that wants to pay loans out electronically funds a float with the platform,
because an outbound transfer can only draw on a balance held at the PSP. The new
wording keeps the part that is still true and still matters — collections never
pass through us — and is explicit about the exception.

**Matched on a substring, not on equality.** Elsewhere in this project (see
0014) a guarded update compares against the exact old default so that
hand-edited copy is left alone. That is the right instinct for an email address;
it is the wrong one here. A row still containing "never hold member funds in a
platform wallet" is making a false statement to every visitor whether or not
somebody edited the sentences around it, so any row carrying that phrase is
rewritten. Copy that no longer contains it is untouched.
"""
from django.db import migrations

OLD_BODY_PHRASE = "never hold member funds in a platform wallet"
OLD_TITLE_PHRASE = "not custodian"

OLD_TITLE = "Ledger, not custodian."
OLD_BODY = (
    "Money moves through licensed payment providers into your cooperative's "
    "own bank account. We record, reconcile and report on it — we never hold "
    "member funds in a platform wallet. That is a deliberate engineering and "
    "regulatory choice, and it is why your money never depends on us staying "
    "solvent."
)

NEW_TITLE = "Ledger first. Custodian only when you ask."
NEW_BODY = (
    "Collections settle through licensed payment providers straight into your "
    "cooperative's own bank account — we record, reconcile and report on them, "
    "and that money never passes through us. The only funds we ever hold are a "
    "disbursement float you deposit yourself, when you choose to lend from it: "
    "yours to see, to top up, and to withdraw whenever you want it back."
)


def retire_claim(apps, schema_editor):
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteContent.objects.filter(
        principle_body__contains=OLD_BODY_PHRASE,
    ).update(principle_body=NEW_BODY)
    SiteContent.objects.filter(
        principle_title__contains=OLD_TITLE_PHRASE,
    ).update(principle_title=NEW_TITLE)


def restore_claim(apps, schema_editor):
    """Reverse for completeness. Note this puts an untrue statement back on the
    public site, so it is only ever appropriate alongside reverting the wallet
    itself."""
    SiteContent = apps.get_model("platform_admin", "SiteContent")
    SiteContent.objects.filter(principle_body=NEW_BODY).update(
        principle_body=OLD_BODY)
    SiteContent.objects.filter(principle_title=NEW_TITLE).update(
        principle_title=OLD_TITLE)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0018_alter_sitecontent_principle_body_and_more"),
    ]

    operations = [
        migrations.RunPython(retire_claim, restore_claim),
    ]
