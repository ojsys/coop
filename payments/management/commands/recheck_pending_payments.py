"""
Ask the provider about every pending online payment and apply the answer.

Wallet top-ups and online loan repayments used to be settled only when the payer
came back through the checkout's return URL. One who paid and closed the tab
left the record PENDING — an uncredited wallet, or a member still shown in
arrears — although the provider held the money. This finds those and settles
them through the same services the admin actions, the webhook and the apps use.

    python manage.py recheck_pending_payments --dry-run
    python manage.py recheck_pending_payments

Safe to repeat and to schedule: a settled record is never touched again, and one
that is still unpaid simply stays pending. Member-reported bank transfers are
left alone — they are checked against the society's account, not the provider.

``--audit-confirmed`` also goes the other way: it asks Paystack about every
*confirmed* online loan repayment and reverses those Paystack has no successful
payment for, returning the loan to the member to repay. Run it with --dry-run
first. If the Paystack key now belongs to a different account from the one that
took the payments, every genuine repayment would read as "no record", so check
that the dry run only names the repayments you expect.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from loans.models import LoanRepayment
from payments.models import Provider, WalletTopUp
from payments.providers import PaymentInitError, get_provider
from payments import services
from payments.services import (PaymentNotReceived, WalletError,
                               recheck_settled_repayment, recheck_wallet_topup,
                               verify_loan_payment)


class Command(BaseCommand):
    help = ("Settle pending wallet top-ups and online loan repayments the "
            "provider reports as paid.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what the provider says without writing anything.",
        )
        parser.add_argument(
            "--allow-simulated", action="store_true",
            help="Run even without a live provider key (dev only: every "
                 "pending payment will be treated as paid).",
        )
        parser.add_argument(
            "--audit-confirmed", action="store_true",
            help="Also check confirmed online loan repayments and reverse any "
                 "Paystack has no successful payment for.",
        )

    def handle(self, *args, **options):
        # Looked up at call time, not bound at import, so the guard can never
        # be a stale reference.
        if services.provider_is_simulated() and not options["allow_simulated"]:
            self.stderr.write(self.style.ERROR(
                "No live PAYSTACK_SECRET_KEY is configured, so the provider "
                "would report every payment as paid. Refusing to settle on "
                "that. Pass --allow-simulated on a dev database only."))
            return

        self.dry_run = options["dry_run"]
        self._topups()
        self._repayments()
        if options["audit_confirmed"]:
            self._audit_confirmed()
        if self.dry_run:
            self.stdout.write(self.style.NOTICE("Dry run — nothing written."))

    def _ask(self, provider, reference):
        detail = get_provider(provider).fetch_transaction(reference)
        return (detail or {}).get("status") or "unknown"

    def _topups(self):
        pending = (WalletTopUp.all_objects
                   .filter(status=WalletTopUp.Status.PENDING)
                   .select_related("cooperative").order_by("created_at"))
        self.stdout.write(f"Wallet top-ups pending: {len(pending)}")

        confirmed = failed = waiting = errors = 0
        for topup in pending:
            label = (f"{topup.psp_reference}  {topup.cooperative}  "
                     f"{topup.amount:,.2f}")
            try:
                if self.dry_run:
                    status = self._ask(topup.provider, topup.psp_reference)
                    self.stdout.write(f"  {label}  provider says: {status}")
                    continue
                topup = recheck_wallet_topup(topup)
            except (PaymentInitError, WalletError) as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  {label}  {exc}"))
                continue

            if topup.is_confirmed:
                confirmed += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  {label}  confirmed, {topup.net_amount:,.2f} credited "
                    f"(fee {topup.fee:,.2f})"))
            elif topup.status == WalletTopUp.Status.FAILED:
                failed += 1
                self.stdout.write(f"  {label}  failed at the provider")
            else:
                waiting += 1
                self.stdout.write(f"  {label}  still unpaid")

        if not self.dry_run and pending:
            self.stdout.write(
                f"  {confirmed} confirmed, {failed} failed, {waiting} still "
                f"pending, {errors} could not be checked.")

    def _repayments(self):
        pending = (LoanRepayment.all_objects
                   .filter(status=LoanRepayment.Status.PENDING,
                           channel=LoanRepayment.Channel.PSP)
                   .exclude(psp_reference="")
                   .select_related("cooperative").order_by("created_at"))
        self.stdout.write(f"Online loan repayments pending: {len(pending)}")

        settled = unapplied = waiting = errors = 0
        for repayment in pending:
            reference = repayment.psp_reference
            label = (f"{reference}  {repayment.cooperative}  loan "
                     f"#{repayment.loan_id}  {repayment.amount:,.2f}")
            try:
                if self.dry_run:
                    status = self._ask(Provider.PAYSTACK, reference)
                    self.stdout.write(f"  {label}  provider says: {status}")
                    continue
                result = verify_loan_payment(repayment.cooperative, reference)
            except PaymentNotReceived as exc:
                waiting += 1
                self.stdout.write(f"  {label}  not paid — {exc}")
                continue
            except PaymentInitError as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  {label}  {exc}"))
                continue

            if result is None:
                unapplied += 1
                self.stdout.write(self.style.WARNING(
                    f"  {label}  paid, but the loan was already fully repaid "
                    f"— nothing posted; refund the member"))
            elif result.status == LoanRepayment.Status.CONFIRMED:
                settled += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  {label}  confirmed, {result.amount:,.2f} posted"))
            else:
                waiting += 1
                self.stdout.write(f"  {label}  still unpaid")

        if not self.dry_run and pending:
            self.stdout.write(
                f"  {settled} confirmed, {unapplied} paid after payoff, "
                f"{waiting} still pending, {errors} could not be checked.")

    def _audit_confirmed(self):
        confirmed = (LoanRepayment.all_objects
                     .filter(status=LoanRepayment.Status.CONFIRMED,
                             channel=LoanRepayment.Channel.PSP)
                     .exclude(psp_reference="")
                     .select_related("cooperative").order_by("created_at"))
        self.stdout.write(
            f"Confirmed online loan repayments to audit: {len(confirmed)}")

        kept = reversed_ = short = errors = 0
        for repayment in confirmed:
            reference = repayment.psp_reference
            label = (f"{reference}  {repayment.cooperative}  loan "
                     f"#{repayment.loan_id}  {repayment.amount:,.2f}")
            try:
                if self.dry_run:
                    status = self._ask(Provider.PAYSTACK, reference)
                    verdict = ("keep" if status == "success"
                               else "WOULD REVERSE")
                    self.stdout.write(
                        f"  {label}  provider says: {status}  → {verdict}")
                    continue
                repayment, outcome = recheck_settled_repayment(repayment)
            except PaymentInitError as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f"  {label}  {exc}"))
                continue

            if outcome == "reversed":
                reversed_ += 1
                self.stdout.write(self.style.WARNING(
                    f"  {label}  reversed — {repayment.reversal_reason}"))
            elif outcome == "short":
                short += 1
                self.stdout.write(self.style.WARNING(
                    f"  {label}  Paystack received less — check by hand"))
            else:
                kept += 1

        if not self.dry_run and confirmed:
            self.stdout.write(
                f"  {kept} verified, {reversed_} reversed, {short} short, "
                f"{errors} could not be checked.")
