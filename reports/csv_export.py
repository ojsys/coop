"""CSV export helpers for the console (ledger, contributions, reports)."""
from __future__ import annotations

import csv

from django.http import HttpResponse


def csv_response(filename: str, header: list[str], rows) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return response


def ledger_entries_csv(queryset) -> HttpResponse:
    header = ["date", "reference", "account_code", "account_name", "member_no",
              "debit", "credit", "currency", "description"]
    rows = (
        [
            e.journal.occurred_at.date() if e.journal.occurred_at else "",
            e.journal.reference, e.account.code, e.account.name,
            (e.membership.member_no if e.membership_id else ""),
            e.debit, e.credit, e.currency, e.description,
        ]
        for e in queryset.select_related("account", "membership", "journal")
    )
    return csv_response("ledger-entries.csv", header, rows)


def contributions_csv(queryset) -> HttpResponse:
    header = ["date", "member_no", "type", "amount", "channel", "status",
              "reference"]
    rows = (
        [
            c.occurred_at.date() if c.occurred_at else "",
            c.membership.member_no, c.contribution_type.name, c.amount,
            c.channel, c.status, c.psp_reference,
        ]
        for c in queryset.select_related("membership", "contribution_type")
    )
    return csv_response("contributions.csv", header, rows)


def arrears_csv(report: dict) -> HttpResponse:
    header = ["member_no", "expected", "collected", "outstanding"]
    rows = (
        [i["member_no"], i["expected"], i["collected"], i["outstanding"]]
        for i in report.get("items", [])
    )
    return csv_response("arrears.csv", header, rows)


def contribution_summary_csv(summary: dict) -> HttpResponse:
    header = ["type", "total", "count"]
    rows = (
        [r["type"], r["total"], r["count"]]
        for r in summary.get("by_type", [])
    )
    return csv_response("contribution-summary.csv", header, rows)
