"""
Reporting & dashboards (PRD §6.5). Pure aggregation over the ledger and
contributions — no stored report state, everything is derived on demand so the
numbers always reconcile with the immutable ledger.
"""
from __future__ import annotations

import calendar
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import Membership
from contributions.models import Contribution, ContributionType
from ledger.models import Account, LedgerEntry

ZERO = Decimal("0.00")


def month_bounds(year: int, month: int):
    """Return timezone-aware [start, end) datetimes for a calendar month."""
    start = timezone.datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
    last_day = calendar.monthrange(year, month)[1]
    end_day = timezone.datetime(year, month, last_day, 23, 59, 59,
                                tzinfo=timezone.get_current_timezone())
    # end is exclusive-ish; callers filter __lte end_day.
    return start, end_day


def _confirmed(coop, start=None, end=None):
    qs = Contribution.all_objects.filter(
        cooperative=coop, status=Contribution.Status.CONFIRMED,
    )
    if start is not None:
        qs = qs.filter(occurred_at__gte=start)
    if end is not None:
        qs = qs.filter(occurred_at__lte=end)
    return qs


def total_savings(coop) -> Decimal:
    """Sum of all member funds held (liability-account balances)."""
    agg = LedgerEntry.all_objects.filter(
        cooperative=coop, account__kind=Account.Kind.LIABILITY,
    ).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["c"] or ZERO) - (agg["d"] or ZERO)


def arrears_report(coop, *, year: int, month: int) -> dict:
    """Members behind on mandatory periodic contributions this period."""
    start, end = month_bounds(year, month)
    mandatory = list(ContributionType.all_objects.filter(
        cooperative=coop, active=True,
        kind=ContributionType.Kind.MANDATORY,
        frequency__in=[ContributionType.Frequency.MONTHLY,
                       ContributionType.Frequency.WEEKLY],
        expected_amount__isnull=False,
    ))
    expected_per_member = sum((t.expected_amount for t in mandatory), ZERO)

    members = Membership.all_objects.filter(
        cooperative=coop, status=Membership.Status.ACTIVE,
    )
    type_ids = [t.id for t in mandatory]

    behind = []
    total_outstanding = ZERO
    for member in members:
        collected = _confirmed(coop, start, end).filter(
            membership=member, contribution_type_id__in=type_ids,
        ).aggregate(s=Sum("amount"))["s"] or ZERO
        shortfall = expected_per_member - collected
        if shortfall > 0:
            total_outstanding += shortfall
            behind.append({
                "membership_id": member.id,
                "member_no": member.member_no,
                "expected": expected_per_member,
                "collected": collected,
                "outstanding": shortfall,
            })
    return {
        "period": f"{year}-{month:02d}",
        "expected_per_member": expected_per_member,
        "members_in_arrears": len(behind),
        "total_outstanding": total_outstanding,
        "items": behind,
    }


def contribution_summary(coop, *, year: int, month: int) -> dict:
    """Collected amount broken down by contribution type for a period."""
    start, end = month_bounds(year, month)
    rows = (
        _confirmed(coop, start, end)
        .values("contribution_type__name")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    )
    by_type = [
        {"type": r["contribution_type__name"], "total": r["total"],
         "count": r["count"]}
        for r in rows
    ]
    return {
        "period": f"{year}-{month:02d}",
        "total": sum((r["total"] for r in by_type), ZERO),
        "by_type": by_type,
    }


def contributions_trend(coop, *, months: int = 6) -> list:
    """Collected total per month for the last ``months`` months (oldest first)."""
    today = timezone.now()
    year, month = today.year, today.month
    series = []
    for _ in range(months):
        start, end = month_bounds(year, month)
        total = _confirmed(coop, start, end).aggregate(
            s=Sum("amount"))["s"] or ZERO
        series.append({"period": f"{year}-{month:02d}", "total": total})
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(series))


def dashboard(coop, *, year: int = None, month: int = None) -> dict:
    """The society dashboard headline metrics (PRD §6.5 / design 'Dashboard')."""
    from payments.services import reconciliation_summary

    now = timezone.now()
    year = year or now.year
    month = month or now.month
    start, end = month_bounds(year, month)

    period_qs = _confirmed(coop, start, end)
    period_total = period_qs.aggregate(s=Sum("amount"))["s"] or ZERO

    members = Membership.all_objects.filter(cooperative=coop)
    active = members.filter(status=Membership.Status.ACTIVE).count()
    total_members = members.count()
    contributors = period_qs.values("membership").distinct().count()
    participation = round((contributors / active * 100), 1) if active else 0.0

    arrears = arrears_report(coop, year=year, month=month)

    return {
        "period": f"{year}-{month:02d}",
        "contributions_this_period": period_total,
        "total_savings": total_savings(coop),
        "arrears_outstanding": arrears["total_outstanding"],
        "members_in_arrears": arrears["members_in_arrears"],
        "active_members": active,
        "total_members": total_members,
        "participation_rate": participation,
        "contributions_trend": contributions_trend(coop),
        "reconciliation": reconciliation_summary(coop),
    }


def send_arrears_reminders(cooperative, *, year=None, month=None):
    """Notify every member behind on mandatory dues for the period.

    Reuses the arrears report and the in-app notification channel. Returns the
    number of members reached.
    """
    from django.utils import timezone

    from accounts.models import Membership
    from communications.models import Notification
    from communications.services import notify

    now = timezone.now()
    report = arrears_report(cooperative, year=year or now.year,
                            month=month or now.month)
    reached = 0
    for item in report["items"]:
        membership = (Membership.all_objects
                      .filter(cooperative=cooperative, id=item["membership_id"])
                      .select_related("user").first())
        if membership is None:
            continue
        notify(membership, kind=Notification.Kind.CONTRIBUTION,
               title="Contribution reminder",
               body=(f"Your outstanding dues for this period are "
                     f"N{item['outstanding']:,.2f}. Please pay to stay in good "
                     f"standing."))
        reached += 1
    return {
        "reached": reached,
        "members_in_arrears": report["members_in_arrears"],
        "total_outstanding": report["total_outstanding"],
    }
