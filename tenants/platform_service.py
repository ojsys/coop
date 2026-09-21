"""
Cross-tenant analytics for the Startup Ripple platform surface.

Unlike the per-cooperative ``reports`` module, these aggregate *across all
tenants* — so they deliberately use unscoped managers (``all_objects`` /
``Cooperative.objects``) and run only for platform admins. Money never moves
here; this is read-only reporting over what the tenants have recorded.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import Membership
from contributions.models import Contribution
from payments.models import PaymentEvent
from reports.services import month_bounds
from tenants.models import Cooperative

ZERO = Decimal("0.00")

# Indicative monthly price per plan tier (midpoints of the PRD pricing bands).
# MRR is derived from each active cooperative's actual tier, so it moves as the
# real tenant book changes — not a hard-coded figure.
TIER_MRR = {
    Cooperative.Tier.SMALL: Decimal("10000"),
    Cooperative.Tier.MEDIUM: Decimal("35000"),
    Cooperative.Tier.LARGE: Decimal("150000"),
}


def _confirmed_all(start=None, end=None):
    qs = Contribution.all_objects.filter(status=Contribution.Status.CONFIRMED)
    if start is not None:
        qs = qs.filter(occurred_at__gte=start)
    if end is not None:
        qs = qs.filter(occurred_at__lte=end)
    return qs


def _platform_reconciliation() -> float:
    counts = {status: 0 for status, _ in PaymentEvent.Status.choices}
    for row in PaymentEvent.all_objects.values("status"):
        counts[row["status"]] += 1
    matched = counts.get(PaymentEvent.Status.MATCHED, 0)
    considered = sum(counts.values()) - counts.get(PaymentEvent.Status.IGNORED, 0)
    return round((matched / considered * 100), 1) if considered else 100.0


def _contributions_trend(months: int = 6) -> list:
    today = timezone.now()
    year, month = today.year, today.month
    series = []
    for _ in range(months):
        start, end = month_bounds(year, month)
        total = _confirmed_all(start, end).aggregate(s=Sum("amount"))["s"] or ZERO
        series.append({"period": f"{year}-{month:02d}", "total": total})
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(series))


def platform_overview() -> dict:
    """Headline cross-tenant metrics for the platform command center."""
    now = timezone.now()
    start, end = month_bounds(now.year, now.month)

    coops = Cooperative.objects.all()
    active = coops.exclude(status=Cooperative.Status.CLOSED)

    # Prefer real billing: sum of active subscription plan prices. Fall back to
    # the tier-based estimate only while no subscriptions have been created yet.
    from platform_admin.services import subscription_mrr

    mrr = subscription_mrr()
    if mrr == ZERO:
        mrr = sum((TIER_MRR.get(c.tier, ZERO) for c in active), ZERO)

    by_state = [
        {"state": r["state"] or "Unspecified", "count": r["c"]}
        for r in coops.values("state").annotate(c=Count("id")).order_by("-c")
    ]
    by_tier = {
        r["tier"]: r["c"]
        for r in coops.values("tier").annotate(c=Count("id"))
    }
    recently_provisioned = [
        {
            "name": c.name,
            "state": c.state or "—",
            "created_at": c.created_at.isoformat(),
        }
        for c in coops.order_by("-created_at")[:5]
    ]

    gross_contributions = _confirmed_all(start, end).aggregate(s=Sum("amount"))["s"] or ZERO

    return {
        "period": f"{now.year}-{now.month:02d}",
        "cooperatives_live": active.count(),
        "cooperatives_total": coops.count(),
        "members_total": Membership.all_objects.count(),
        "active_members_total": Membership.all_objects.filter(
            status=Membership.Status.ACTIVE,
        ).count(),
        "gross_contributions": gross_contributions,
        "mrr": mrr,
        "reconciliation_accuracy": _platform_reconciliation(),
        "by_state": by_state,
        "by_tier": {
            "small": by_tier.get(Cooperative.Tier.SMALL, 0),
            "medium": by_tier.get(Cooperative.Tier.MEDIUM, 0),
            "large": by_tier.get(Cooperative.Tier.LARGE, 0),
        },
        "recently_provisioned": recently_provisioned,
        "contributions_trend": _contributions_trend(),
    }
