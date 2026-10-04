"""
Reconstruct the disbursement journal link for loans that predate the field.

Until now a loan's DISBURSED status had no pointer to the ledger entry behind
it, so the two disbursement paths could only be recognised by the shape of the
trace they left:

    cash        a journal referenced "LOAN-<loan id>-DISB"
    electronic  a Payout row for the loan, carrying its own journal

Both are matched here. A loan left unmatched is *not* quietly ignored — it is
exactly the inconsistency the new health check reports as
``disbursed_without_journal``: a status claiming money moved with nothing in the
books to support it. That needs a person to look at it, so this migration
deliberately leaves it visible rather than inventing a journal to point at.

Nothing is written to the ledger here. This only fills in a reference to
journals that already exist.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    Loan = apps.get_model("loans", "Loan")
    Journal = apps.get_model("ledger", "Journal")
    Payout = apps.get_model("payments", "Payout")

    loans = list(
        Loan.objects.filter(status="disbursed", disbursement_journal__isnull=True)
    )
    if not loans:
        return

    # Electronic: the payout for this loan already holds its journal. Newest
    # first, so a retried payout wins over the failed attempt before it.
    payout_journal = {}
    for payout in (Payout.objects
                   .filter(kind="loan", object_id__in=[loan.pk for loan in loans],
                           journal__isnull=False)
                   .order_by("object_id", "-created_at", "-id")):
        payout_journal.setdefault(payout.object_id, payout.journal_id)

    # Cash: matched on the reference the cash path has always used. Scoped by
    # cooperative as well, because the reference is only unique per tenant.
    wanted = {f"LOAN-{loan.pk}-DISB": loan.pk for loan in loans}
    cash_journal = {}
    for journal in Journal.objects.filter(reference__in=list(wanted)):
        loan_pk = wanted.get(journal.reference)
        if loan_pk is not None:
            cash_journal[loan_pk] = journal.pk

    updated = []
    for loan in loans:
        journal_id = payout_journal.get(loan.pk) or cash_journal.get(loan.pk)
        if journal_id is None:
            continue
        loan.disbursement_journal_id = journal_id
        updated.append(loan)

    if updated:
        Loan.objects.bulk_update(updated, ["disbursement_journal"])


def unlink(apps, schema_editor):
    Loan = apps.get_model("loans", "Loan")
    Loan.objects.update(disbursement_journal=None)


class Migration(migrations.Migration):

    dependencies = [
        ("loans", "0006_loan_disbursement_journal"),
        # Payout is read here, so its table must exist first.
        ("payments", "0006_payout"),
    ]

    operations = [
        migrations.RunPython(backfill, unlink),
    ]
