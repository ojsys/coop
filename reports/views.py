from __future__ import annotations

from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import Membership
from core.context import get_current_cooperative
from core.views import TenantScopedViewMixin
from reports import services
from reports.pdf import member_statement_pdf


class ReportsViewSet(TenantScopedViewMixin, viewsets.ViewSet):
    """Read-only dashboards and reports for the active cooperative."""

    permission_classes = [IsAuthenticated]

    def _period(self, request):
        year = request.query_params.get("year")
        month = request.query_params.get("month")
        return (int(year) if year else None, int(month) if month else None)

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        year, month = self._period(request)
        return Response(services.dashboard(
            get_current_cooperative(), year=year, month=month,
        ))

    @action(detail=False, methods=["get"], url_path="contribution-summary")
    def contribution_summary(self, request):
        from django.utils import timezone
        now = timezone.now()
        year, month = self._period(request)
        return Response(services.contribution_summary(
            get_current_cooperative(),
            year=year or now.year, month=month or now.month,
        ))

    @action(detail=False, methods=["get"])
    def arrears(self, request):
        from django.utils import timezone
        now = timezone.now()
        year, month = self._period(request)
        return Response(services.arrears_report(
            get_current_cooperative(),
            year=year or now.year, month=month or now.month,
        ))

    @action(detail=False, methods=["get"], url_path="trial-balance")
    def trial_balance(self, request):
        from reports import financials
        return Response(financials.trial_balance(get_current_cooperative()))

    @action(detail=False, methods=["get"], url_path="income-statement")
    def income_statement(self, request):
        from reports import financials
        return Response(financials.income_statement(get_current_cooperative()))

    @action(detail=False, methods=["get"], url_path="balance-sheet")
    def balance_sheet(self, request):
        from reports import financials
        return Response(financials.balance_sheet(get_current_cooperative()))

    @action(detail=False, methods=["get"], url_path="trial-balance-csv")
    def trial_balance_csv(self, request):
        from reports import financials
        from reports.csv_export import csv_response
        tb = financials.trial_balance(get_current_cooperative())
        rows = [[r["code"], r["name"], r["kind"], r["debit"], r["credit"]]
                for r in tb["rows"]]
        rows.append(["", "TOTAL", "", tb["total_debit"], tb["total_credit"]])
        return csv_response("trial-balance.csv",
                            ["code", "name", "kind", "debit", "credit"], rows)

    @action(detail=False, methods=["get"], url_path="arrears-csv")
    def arrears_csv(self, request):
        from django.utils import timezone

        from reports.csv_export import arrears_csv
        now = timezone.now()
        year, month = self._period(request)
        report = services.arrears_report(
            get_current_cooperative(),
            year=year or now.year, month=month or now.month)
        return arrears_csv(report)

    @action(detail=False, methods=["get"], url_path="contribution-summary-csv")
    def contribution_summary_csv(self, request):
        from django.utils import timezone

        from reports.csv_export import contribution_summary_csv
        now = timezone.now()
        year, month = self._period(request)
        summary = services.contribution_summary(
            get_current_cooperative(),
            year=year or now.year, month=month or now.month)
        return contribution_summary_csv(summary)

    @action(detail=False, methods=["post"], url_path="send-arrears-reminders")
    def send_arrears_reminders(self, request):
        from django.utils import timezone
        now = timezone.now()
        year, month = self._period(request)
        result = services.send_arrears_reminders(
            get_current_cooperative(),
            year=year or now.year, month=month or now.month)
        return Response(result)

    @action(detail=False, methods=["get"], url_path="statement-pdf")
    def statement_pdf(self, request):
        membership_id = request.query_params.get("membership")
        membership = Membership.objects.filter(pk=membership_id).first()
        if membership is None:
            raise ValidationError("Unknown member.")
        pdf = member_statement_pdf(membership)
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="statement-{membership.member_no}.pdf"'
        )
        return response
