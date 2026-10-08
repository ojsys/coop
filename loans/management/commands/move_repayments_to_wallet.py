"""
Re-book loan repayments that were recorded as cash but arrived elsewhere.

Until this fix every repayment debited Cash (1000). An online (Paystack)
repayment actually lands in the platform's Paystack balance — the disbursement
wallet (1020) — less Paystack's fee, so a repaid loan never became lendable
again. A reported bank transfer landed in the society's bank (1010).

For each such repayment this posts one correcting journal, leaving the
original in place (the ledger is append-only):

    debit  1020 Disbursement Wallet  net      (or 1010 Bank if the charge
    debit  5000 Payment Charges      fee       was split to a subaccount)
    credit 1000 Cash                 gross

    python manage.py move_repayments_to_wallet --dry-run
    python manage.py move_repayments_to_wallet

Safe to re-run: each correction has its own reference and is skipped once
posted. Online repayments need a live Paystack key, because the fee and
where the money settled come from Paystack.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import Account, Journal, LedgerEntry
from ledger.services import Line, post_journal
from loans.models import LoanRepayment
from loans.services import receipt_lines
from payments import services as payment_services
from payments.models import Provider
from payments.providers import PaymentInitError, get_provider


class Command(BaseCommand):
    help = "Move loan repayments booked to Cash into the wallet or bank."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would move without posting.")
        parser.add_argument(
            "--allow-simulated", action="store_true",
            help="Run without a live Paystack key (dev only: fees read as 0).")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        simulated = payment_services.provider_is_simulated()
        if simulated and not options["allow_simulated"]:
            self.stderr.write(self.style.ERROR(
                "No live PAYSTACK_SECRET_KEY is configured, so Paystack's fee "
                "and settlement cannot be read. Configure the key, or pass "
                "--allow-simulated on a dev database."))
            return

        repayments = (LoanRepayment.all_objects
                      .filter(status=LoanRepayment.Status.CONFIRMED,
                              channel__in=[LoanRepayment.Channel.PSP,
                                           LoanRepayment.Channel.TRANSFER],
                              journal__isnull=False)
                      .select_related("loan", "cooperative", "journal")
                      .order_by("created_at"))
        moved = skipped = errors = 0
        total = Decimal("0.00")
        for repayment in repayments:
            reference = f"RPY-{repayment.pk}-MOVE"
            coop = repayment.cooperative
            if Journal.all_objects.filter(cooperative=coop,
                                          reference=reference).exists():
                skipped += 1
                continue
            cash = Account.all_objects.filter(cooperative=coop,
                                              code="1000").first()
            gross = sum((e.debit for e in LedgerEntry.all_objects.filter(
                journal=repayment.journal, account=cash)), Decimal("0.00"))
            if not gross:
                skipped += 1      # already booked where the money landed
                continue

            receipt = None
            if repayment.channel == LoanRepayment.Channel.PSP:
                try:
                    receipt = get_provider(Provider.PAYSTACK).fetch_transaction(
                        repayment.psp_reference)
                except PaymentInitError as exc:
                    errors += 1
                    self.stdout.write(self.style.ERROR(
                        f"  {repayment.psp_reference}: {exc}"))
                    continue

            lines = receipt_lines(repayment.loan, gross,
                                  channel=repayment.channel, receipt=receipt)
            where = ", ".join(f"{line.account.name} {line.debit:,.2f}"
                              for line in lines)
            self.stdout.write(
                f"  {coop.name} · loan #{repayment.loan_id} · "
                f"{repayment.get_channel_display()} {gross:,.2f} → {where}")
            total += gross
            moved += 1
            if dry:
                continue
            with transaction.atomic():
                post_journal(
                    cooperative=coop, reference=reference,
                    memo=(f"Loan {repayment.loan_id} repayment re-booked from "
                          f"Cash to where it was received"),
                    lines=[*lines, Line(account=cash, credit=gross,
                                        description="Re-booked from Cash")])

        verb = "Would move" if dry else "Moved"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {moved} repayment(s), {total:,.2f} in all. "
            f"{skipped} already right, {errors} could not be checked."))
        if dry:
            self.stdout.write(self.style.NOTICE("Dry run — nothing posted."))
