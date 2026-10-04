"""
Report loans whose state does not add up — and, optionally, fix the safe ones.

Read-only by default. ``--fix-safe`` applies only the corrections that cannot
touch the ledger: rebuilding a schedule, rechecking a transfer with the
provider, closing a loan that is fully repaid. Anything that would post or
reverse a journal is reported and left alone, because corrections here are made
by reversal with an audit trail and a batch job writing journals is how a ledger
stops being answerable.

    python manage.py check_loans
    python manage.py check_loans --cooperative imole
    python manage.py check_loans --fix-safe
    python manage.py check_loans --json

Loans that are approved but not yet paid are listed separately and are **not**
counted as defects: before approval began disbursing, that was simply the normal
state, so those rows are a work queue rather than damage.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from loans.health import AWAITING_PAYMENT, fix_finding, loan_health

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class Command(BaseCommand):
    help = "Check loans for inconsistent state; optionally fix the safe ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cooperative",
            help="Limit to one cooperative, by id or slug. Omit for all.",
        )
        parser.add_argument(
            "--fix-safe", action="store_true",
            help="Apply the corrections that cannot touch the ledger.",
        )
        parser.add_argument(
            "--json", action="store_true",
            help="Machine-readable output, for piping or a scheduled check.",
        )

    def handle(self, *args, **options):
        cooperative = self._resolve(options.get("cooperative"))
        report = loan_health(cooperative)

        if options["json"]:
            self.stdout.write(json.dumps({
                **{k: v for k, v in report.items() if k != "findings"},
                "findings": [f.as_dict() for f in report["findings"]],
            }, indent=2, default=str))
            if options["fix_safe"]:
                self._fix(report)
            return

        self._print(report, cooperative)
        if options["fix_safe"]:
            self._fix(report)
        elif report["auto_fixable"]:
            self.stdout.write(self.style.NOTICE(
                f"\n{report['auto_fixable']} of these can be corrected "
                f"automatically — re-run with --fix-safe."))

    # ── helpers ─────────────────────────────────────────────────────────────
    def _resolve(self, value):
        if not value:
            return None
        from tenants.models import Cooperative

        lookup = {"pk": value} if str(value).isdigit() else {"slug": value}
        coop = Cooperative.objects.filter(**lookup).first()
        if coop is None:
            raise CommandError(f"No cooperative matching {value!r}.")
        return coop

    def _print(self, report, cooperative):
        scope = cooperative.name if cooperative else "every cooperative"
        self.stdout.write(f"Checked {report['checked']} loans across {scope}.\n")

        defects = [f for f in report["findings"] if f.code != AWAITING_PAYMENT]
        waiting = [f for f in report["findings"] if f.code == AWAITING_PAYMENT]

        if not defects:
            self.stdout.write(self.style.SUCCESS(
                "No inconsistent loans found."))
        else:
            self.stdout.write(self.style.ERROR(
                f"{len(defects)} inconsistent loan(s) "
                f"({report['critical']} critical):\n"))
            for finding in sorted(defects,
                                  key=lambda f: SEVERITY_ORDER[f.severity]):
                mark = ("CRITICAL" if finding.severity == "critical"
                        else "warning")
                self.stdout.write(
                    f"  [{mark}] loan {finding.loan_id} · {finding.member} · "
                    f"{finding.amount}")
                self.stdout.write(f"      {finding.title}")
                self.stdout.write(f"      {finding.detail}")
                self.stdout.write(f"      fix: {finding.fix}"
                                  + ("" if finding.auto_fixable
                                     else "  (needs a person)"))
                self.stdout.write("")

        if waiting:
            self.stdout.write(self.style.WARNING(
                f"\n{len(waiting)} approved loan(s) awaiting payment — a work "
                f"queue, not a fault:"))
            for finding in waiting:
                self.stdout.write(
                    f"  loan {finding.loan_id} · {finding.member} · "
                    f"{finding.amount} — {finding.detail}")

    def _fix(self, report):
        fixable = [f for f in report["findings"] if f.auto_fixable]
        if not fixable:
            self.stdout.write("\nNothing to fix automatically.")
            return

        self.stdout.write(f"\nApplying {len(fixable)} safe correction(s):")
        for finding in fixable:
            try:
                result = fix_finding(finding)
            except Exception as exc:                # noqa: BLE001
                # One bad row must not abort the sweep, and the reason has to
                # reach the operator rather than a traceback halfway down.
                self.stdout.write(self.style.ERROR(
                    f"  loan {finding.loan_id}: FAILED — {exc}"))
                continue
            self.stdout.write(self.style.SUCCESS(
                f"  loan {finding.loan_id}: {result}"))
