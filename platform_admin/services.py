"""
Computed platform analytics that draw on the platform-ops tables (billing,
domains, health) *and* the real tenant book (contributions, reconciliation,
governance). Everything here is read-only aggregation for platform admins.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from accounts.models import Membership
from communications.models import Announcement
from contributions.models import Contribution
from governance.models import Resolution
from payments.models import PaymentEvent
from platform_admin.models import Domain, Invoice, Subscription
from tenants.models import Cooperative

ZERO = Decimal("0.00")


# ── Onboarding ──────────────────────────────────────────────────────────────
def start_onboarding(cooperative, owner=None):
    """Create the onboarding pipeline item for a freshly provisioned tenant.

    Idempotent per cooperative. Returns the OnboardingItem.
    """
    from platform_admin.models import OnboardingItem

    item, _ = OnboardingItem.objects.get_or_create(
        cooperative=cooperative,
        defaults={"stage": OnboardingItem.Stage.DISCOVERY,
                  "prospect_name": cooperative.name, "owner": owner},
    )
    return item


def notify_go_live(item, *, force: bool = False) -> bool:
    """Send the go-live announcement for one onboarding item.

    The single path used by the automatic send, the admin's resend action and
    the management command, so all three behave identically.

    Sends once by default: ``advance`` dispatches this on commit, and calling
    it again must not mail the cooperative twice. ``force=True`` is the resend
    — for the case that matters in practice, where the message went out but
    never arrived.

    Never raises. A cooperative that is live but un-announced is a nuisance; an
    exception here escaping into an activation would be worse.
    """
    from django.utils import timezone

    from communications.email import send_go_live_email

    if item.cooperative_id is None:
        return False
    if item.golive_email_sent_at and not force:
        return False

    sent = send_go_live_email(item.cooperative)
    if sent:
        item.golive_email_sent_at = timezone.now()
        item.save(update_fields=["golive_email_sent_at", "updated_at"])
    return sent


# ── Invoice numbering ───────────────────────────────────────────────────────
def next_invoice_number() -> str:
    """Suggest the next ``INV-YYYY-N`` number for the current year.

    Continues the highest sequence seen this year, so numbers are contiguous
    and human-friendly. The number remains editable by the admin.
    """
    import re

    year = timezone.now().year
    prefix = f"INV-{year}-"
    highest = 0
    for number in (Invoice.objects.filter(number__startswith=prefix)
                   .values_list("number", flat=True)):
        m = re.search(r"(\d+)$", number or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{prefix}{highest + 1:03d}"


# ── Billing ─────────────────────────────────────────────────────────────────
def subscription_mrr() -> Decimal:
    """Real MRR: sum of the plan price of every non-canceled subscription."""
    total = (
        Subscription.objects
        .exclude(status=Subscription.Status.CANCELED)
        .aggregate(s=Sum("plan__price_monthly"))["s"]
    )
    return total or ZERO


def billing_summary() -> dict:
    invoices = Invoice.objects.all()
    by_status = {
        r["status"]: {"count": r["c"], "amount": r["a"] or ZERO}
        for r in invoices.values("status").annotate(c=Count("id"),
                                                     a=Sum("amount"))
    }
    outstanding = (
        invoices.filter(status__in=[Invoice.Status.SENT,
                                    Invoice.Status.OVERDUE])
        .aggregate(s=Sum("amount"))["s"] or ZERO
    )
    overdue = (
        invoices.filter(status=Invoice.Status.OVERDUE)
        .aggregate(s=Sum("amount"), c=Count("id"))
    )

    # Coops per plan tier, joined through their subscription.
    plan_rows = (
        Subscription.objects
        .exclude(status=Subscription.Status.CANCELED)
        .values("plan__id", "plan__name", "plan__tier",
                "plan__price_monthly")
        .annotate(coops=Count("id"))
        .order_by("plan__price_monthly")
    )
    plan_tiers = [
        {
            "plan_id": r["plan__id"],
            "name": r["plan__name"],
            "tier": r["plan__tier"],
            "price_monthly": r["plan__price_monthly"],
            "coops": r["coops"],
        }
        for r in plan_rows
    ]

    return {
        "mrr": subscription_mrr(),
        "outstanding": outstanding,
        "overdue_amount": overdue["s"] or ZERO,
        "overdue_count": overdue["c"] or 0,
        "by_status": {
            k: {"count": v["count"], "amount": v["amount"]}
            for k, v in by_status.items()
        },
        "plan_tiers": plan_tiers,
    }


# ── Feature adoption / analytics ────────────────────────────────────────────
def _pct(part: int, whole: int) -> int:
    return round(part / whole * 100) if whole else 0


def feature_adoption() -> list:
    """Share of live cooperatives actually using each capability."""
    coops = Cooperative.objects.exclude(status=Cooperative.Status.CLOSED)
    total = coops.count()

    with_contrib = (
        Contribution.all_objects
        .filter(status=Contribution.Status.CONFIRMED)
        .values("cooperative").distinct().count()
    )
    with_recon = (
        PaymentEvent.all_objects
        .filter(status=PaymentEvent.Status.MATCHED)
        .values("cooperative").distinct().count()
    )
    with_gov = Resolution.all_objects.values("cooperative").distinct().count()
    with_reach = (
        Announcement.all_objects
        .exclude(channels=[])
        .values("cooperative").distinct().count()
    )
    with_domain = (
        Domain.objects.filter(dns_status=Domain.DNSStatus.VERIFIED)
        .values("cooperative").distinct().count()
    )

    return [
        {"feature": "Contributions & ledger", "pct": _pct(with_contrib, total)},
        {"feature": "Reconciliation", "pct": _pct(with_recon, total)},
        {"feature": "Governance & voting", "pct": _pct(with_gov, total)},
        {"feature": "SMS / USSD reach", "pct": _pct(with_reach, total)},
        {"feature": "White-label domain", "pct": _pct(with_domain, total)},
    ]


def churn_watchlist(limit: int = 10) -> list:
    """Cooperatives showing risk signals: overdue invoices or gone quiet."""
    now = timezone.now()
    cutoff = now - timedelta(days=30)
    watch = []
    for coop in Cooperative.objects.exclude(status=Cooperative.Status.CLOSED):
        reasons = []
        score = 0

        overdue = coop.invoices.filter(status=Invoice.Status.OVERDUE).count()
        if overdue:
            reasons.append(f"{overdue} invoice(s) overdue")
            score += 40

        last_contrib = (
            Contribution.all_objects
            .filter(cooperative=coop, status=Contribution.Status.CONFIRMED)
            .order_by("-occurred_at").values_list("occurred_at", flat=True)
            .first()
        )
        if last_contrib is None:
            reasons.append("no confirmed contributions yet")
            score += 30
        elif last_contrib < cutoff:
            days = (now - last_contrib).days
            reasons.append(f"no activity in {days} days")
            score += 35

        active_members = Membership.all_objects.filter(
            cooperative=coop, status=Membership.Status.ACTIVE).count()
        if active_members == 0:
            reasons.append("no active members")
            score += 20

        if reasons:
            watch.append({
                "cooperative_id": coop.id,
                "name": coop.name,
                "reason": " · ".join(reasons),
                "score": min(score, 100),
            })

    watch.sort(key=lambda w: w["score"], reverse=True)
    return watch[:limit]


# ── Time windows ────────────────────────────────────────────────────────────
def _month_window(months: int):
    """Return an ascending list of (year, month, 'Mon') for the last N months."""
    now = timezone.now()
    year, month = now.year, now.month
    out = []
    for _ in range(months):
        label = timezone.datetime(year, month, 1).strftime("%b")
        out.append((year, month, label))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(out))


def _month_end(year, month):
    from reports.services import month_bounds
    return month_bounds(year, month)[1]


# ── Revenue ─────────────────────────────────────────────────────────────────
def mrr_trend(months: int = 6) -> list:
    """MRR at each month-end = plan price of every subscription that had started
    by then and isn't canceled."""
    subs = list(
        Subscription.objects
        .exclude(status=Subscription.Status.CANCELED)
        .values("started_at", "plan__price_monthly")
    )
    series = []
    for year, month, label in _month_window(months):
        end = _month_end(year, month).date()
        total = sum(
            (s["plan__price_monthly"] or ZERO)
            for s in subs
            if s["started_at"] is None or s["started_at"] <= end
        )
        series.append({"period": f"{year}-{month:02d}", "month": label,
                       "mrr": total})
    return series


def revenue_by_tier() -> list:
    rows = (
        Subscription.objects
        .exclude(status=Subscription.Status.CANCELED)
        .values("plan__tier")
        .annotate(coops=Count("id"), revenue=Sum("plan__price_monthly"))
        .order_by("plan__price_monthly")
    )
    return [
        {"tier": r["plan__tier"], "coops": r["coops"],
         "revenue": r["revenue"] or ZERO}
        for r in rows
    ]


def arpu() -> Decimal:
    mrr = subscription_mrr()
    n = (Subscription.objects
         .exclude(status=Subscription.Status.CANCELED).count())
    return round(mrr / n, 2) if n else ZERO


def paid_vs_trial() -> dict:
    paid = Subscription.objects.filter(
        status__in=[Subscription.Status.ACTIVE,
                    Subscription.Status.PAST_DUE]).count()
    trial = Subscription.objects.filter(
        status=Subscription.Status.TRIAL).count()
    return {"paid": paid, "trial": trial}


# ── Growth & activation ─────────────────────────────────────────────────────
def growth_trend(months: int = 6) -> list:
    series = []
    for year, month, label in _month_window(months):
        from reports.services import month_bounds
        start, end = month_bounds(year, month)
        new_coops = Cooperative.objects.filter(
            created_at__gte=start, created_at__lte=end).count()
        new_members = Membership.all_objects.filter(
            created_at__gte=start, created_at__lte=end).count()
        series.append({"period": f"{year}-{month:02d}", "month": label,
                       "new_coops": new_coops, "new_members": new_members})
    return series


def activation_funnel() -> list:
    coops = Cooperative.objects.exclude(status=Cooperative.Status.CLOSED)
    provisioned = coops.count()
    with_contrib = (
        Contribution.all_objects
        .filter(status=Contribution.Status.CONFIRMED)
        .values("cooperative").distinct().count()
    )
    reconciled = (
        PaymentEvent.all_objects
        .filter(status=PaymentEvent.Status.MATCHED)
        .values("cooperative").distinct().count()
    )
    return [
        {"stage": "Provisioned", "count": provisioned},
        {"stage": "First contribution", "count": with_contrib},
        {"stage": "First reconciled cycle", "count": reconciled},
    ]


def active_vs_dormant() -> dict:
    now = timezone.now()
    cutoff = now - timedelta(days=30)
    active_ids = set(
        Contribution.all_objects
        .filter(status=Contribution.Status.CONFIRMED, occurred_at__gte=cutoff)
        .values_list("cooperative_id", flat=True)
    )
    total = Cooperative.objects.exclude(
        status=Cooperative.Status.CLOSED).count()
    return {"active": len(active_ids), "dormant": max(total - len(active_ids), 0)}


def cohort_retention(cohorts: int = 6) -> dict:
    """Retention grid: rows = provisioning-month cohort, cells = % of that cohort
    that recorded a confirmed contribution in month M+k."""
    from reports.services import month_bounds

    window = _month_window(cohorts)  # ascending
    rows = []
    for ci, (cy, cm, clabel) in enumerate(window):
        cstart, cend = month_bounds(cy, cm)
        cohort_ids = list(
            Cooperative.objects
            .filter(created_at__gte=cstart, created_at__lte=cend)
            .values_list("id", flat=True)
        )
        size = len(cohort_ids)
        values = []
        # offsets 0..(remaining months to now)
        for k in range(len(window) - ci):
            oy, om, _ = window[ci + k]
            ostart, oend = month_bounds(oy, om)
            if size == 0:
                values.append(None)
                continue
            retained = (
                Contribution.all_objects
                .filter(cooperative_id__in=cohort_ids,
                        status=Contribution.Status.CONFIRMED,
                        occurred_at__gte=ostart, occurred_at__lte=oend)
                .values("cooperative_id").distinct().count()
            )
            values.append(round(retained / size * 100))
        rows.append({"cohort": clabel, "size": size, "values": values})
    return {"rows": rows, "span": len(window)}


def analytics(months: int = 6) -> dict:
    coops = Cooperative.objects.exclude(status=Cooperative.Status.CLOSED)
    by_state = [
        {"state": r["state"] or "Unspecified", "count": r["c"]}
        for r in coops.values("state").annotate(c=Count("id")).order_by("-c")
    ]
    return {
        "months": months,
        "feature_adoption": feature_adoption(),
        "churn_watchlist": churn_watchlist(),
        "coops_by_state": by_state,
        "net_revenue_retention": _net_revenue_retention(),
        # Revenue
        "mrr_trend": mrr_trend(months),
        "revenue_by_tier": revenue_by_tier(),
        "arpu": arpu(),
        "paid_vs_trial": paid_vs_trial(),
        # Growth & activation
        "growth_trend": growth_trend(months),
        "activation_funnel": activation_funnel(),
        "active_vs_dormant": active_vs_dormant(),
        # Retention
        "cohort_retention": cohort_retention(months),
    }


def analytics_csv_rows(months: int = 6):
    """Rows for the monthly analytics CSV export."""
    mrr = {r["period"]: r["mrr"] for r in mrr_trend(months)}
    yield ["period", "mrr", "new_coops", "new_members"]
    for g in growth_trend(months):
        yield [g["period"], mrr.get(g["period"], ZERO),
               g["new_coops"], g["new_members"]]


def _net_revenue_retention() -> int:
    """Simple NRR proxy: active-subscription MRR vs. all-subscription MRR."""
    active = (
        Subscription.objects.filter(status=Subscription.Status.ACTIVE)
        .aggregate(s=Sum("plan__price_monthly"))["s"] or ZERO
    )
    total = (
        Subscription.objects.aggregate(s=Sum("plan__price_monthly"))["s"]
        or ZERO
    )
    return round(active / total * 100) if total else 100


# ── Attention feed ──────────────────────────────────────────────────────────
def attention_items() -> list:
    """Actionable alerts for the command center, derived from real state."""
    items = []

    overdue = Invoice.objects.filter(status=Invoice.Status.OVERDUE)
    if overdue.exists():
        amt = overdue.aggregate(s=Sum("amount"))["s"] or ZERO
        items.append({
            "icon": "receipt_long",
            "tone": "gold",
            "title": f"{overdue.count()} invoice(s) overdue — "
                     f"₦{amt:,.0f}",
            "detail": ", ".join(
                overdue.values_list("cooperative__name", flat=True)[:3]),
            "cta": "Chase",
            "link": "/platform/billing",
        })

    pending_dns = Domain.objects.filter(dns_status=Domain.DNSStatus.PENDING)
    if pending_dns.exists():
        items.append({
            "icon": "dns",
            "tone": "neutral",
            "title": f"{pending_dns.count()} custom domain(s) awaiting DNS",
            "detail": ", ".join(pending_dns.values_list("domain", flat=True)[:3]),
            "cta": "Verify",
            "link": "/platform/white-label",
        })

    high_churn = [w for w in churn_watchlist() if w["score"] >= 60]
    if high_churn:
        items.append({
            "icon": "warning",
            "tone": "danger",
            "title": f"{len(high_churn)} cooperative(s) flagged high churn risk",
            "detail": ", ".join(w["name"] for w in high_churn[:3]),
            "cta": "Review",
            "link": "/platform/analytics",
        })

    degraded = _degraded_providers()
    if degraded:
        items.append({
            "icon": "monitor_heart",
            "tone": "danger",
            "title": f"{len(degraded)} provider(s) degraded",
            "detail": ", ".join(degraded),
            "cta": "Inspect",
            "link": "/platform/health",
        })

    return items


# ── Health ──────────────────────────────────────────────────────────────────
def _degraded_providers() -> list:
    from platform_admin.models import ProviderStatus
    return list(
        ProviderStatus.objects
        .exclude(status=ProviderStatus.Status.OPERATIONAL)
        .values_list("name", flat=True)
    )


def cooperative_health(coop) -> dict:
    """Per-tenant data diagnostics (the Support tab 'dry-run'). Reused by the
    fleet rollup."""
    from django.db.models import Sum

    from accounts.models import Membership
    from contributions.models import Contribution
    from ledger.models import Account, LedgerEntry
    from payments.models import PaymentEvent, ProviderAccount

    members_total = Membership.all_objects.filter(cooperative=coop).count()
    active_members = Membership.all_objects.filter(
        cooperative=coop, status=Membership.Status.ACTIVE).count()
    accounts = Account.all_objects.filter(cooperative=coop).count()

    agg = LedgerEntry.all_objects.filter(cooperative=coop).aggregate(
        d=Sum("debit"), c=Sum("credit"))
    debits = agg["d"] or ZERO
    credits = agg["c"] or ZERO
    balanced = debits == credits

    events = PaymentEvent.all_objects.filter(cooperative=coop)
    exception_statuses = [
        PaymentEvent.Status.UNMATCHED, PaymentEvent.Status.PARTIAL,
        PaymentEvent.Status.DUPLICATE,
    ]
    exceptions = events.filter(status__in=exception_statuses).count()
    confirmed = Contribution.all_objects.filter(
        cooperative=coop, status=Contribution.Status.CONFIRMED).count()
    providers = ProviderAccount.all_objects.filter(
        cooperative=coop, connected=True).count()

    checks = [
        {"key": "members", "label": "Members imported",
         "ok": members_total > 0,
         "detail": f"{members_total} members ({active_members} active)"},
        {"key": "chart_of_accounts", "label": "Chart of accounts seeded",
         "ok": accounts > 0, "detail": f"{accounts} accounts"},
        {"key": "ledger_balanced",
         "label": "Ledger balanced (debits = credits)",
         "ok": balanced, "detail": f"Dr {debits} / Cr {credits}"},
        {"key": "psp", "label": "PSP subaccount connected",
         "ok": providers > 0, "detail": f"{providers} connected"},
        {"key": "reconciled", "label": "First cycle reconciled",
         "ok": confirmed > 0 and exceptions == 0,
         "detail": f"{confirmed} confirmed · {exceptions} exceptions"},
    ]
    return {
        "cooperative": coop.name,
        "status": coop.status,
        "healthy": all(c["ok"] for c in checks),
        "checks": checks,
        "metrics": {
            "members_total": members_total,
            "active_members": active_members,
            "accounts": accounts,
            "ledger_balanced": balanced,
            "recon_exceptions": exceptions,
            "confirmed_contributions": confirmed,
        },
    }


def fleet_health() -> dict:
    """Roll the per-tenant health check across every cooperative and surface
    the ones that need attention."""
    coops = Cooperative.objects.exclude(status=Cooperative.Status.CLOSED)
    at_risk = []
    healthy_count = 0
    for coop in coops:
        h = cooperative_health(coop)
        if h["healthy"]:
            healthy_count += 1
        else:
            failing = [c["label"] for c in h["checks"] if not c["ok"]]
            at_risk.append({
                "cooperative_id": coop.id,
                "name": coop.name,
                "status": coop.status,
                "failing": failing,
                "recon_exceptions": h["metrics"]["recon_exceptions"],
                "ledger_balanced": h["metrics"]["ledger_balanced"],
            })
    at_risk.sort(key=lambda x: (not x["ledger_balanced"],
                                x["recon_exceptions"]), reverse=True)
    return {
        "total": coops.count(),
        "healthy": healthy_count,
        "at_risk_count": len(at_risk),
        "at_risk": at_risk,
    }


def payment_monitor(days: int = 14) -> dict:
    """Recent payment-event volume and a daily success-rate trend."""
    now = timezone.now()
    start = now - timedelta(days=days)
    events = PaymentEvent.all_objects.filter(received_at__gte=start)

    exception_statuses = [
        PaymentEvent.Status.UNMATCHED, PaymentEvent.Status.PARTIAL,
        PaymentEvent.Status.DUPLICATE,
    ]
    # Daily trend
    trend = []
    for i in range(days):
        day = (start + timedelta(days=i + 1))
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        day_events = events.filter(received_at__gte=day_start,
                                   received_at__lt=day_end)
        total = day_events.count()
        matched = day_events.filter(
            status=PaymentEvent.Status.MATCHED).count()
        trend.append({
            "day": day_start.strftime("%d %b"),
            "total": total,
            "success": round(matched / total * 100) if total else 100,
        })

    recent = [
        {
            "id": e.id, "provider": e.provider, "reference": e.reference,
            "amount": str(e.amount), "status": e.status,
            "cooperative": (e.cooperative.name if e.cooperative_id else None),
            "received_at": e.received_at.isoformat(),
        }
        for e in events.select_related("cooperative")
        .order_by("-received_at")[:20]
    ]
    total = events.count()
    matched = events.filter(status=PaymentEvent.Status.MATCHED).count()
    return {
        "window_days": days,
        "total_events": total,
        "success_rate": round(matched / total * 100, 1) if total else 100.0,
        "exceptions": events.filter(status__in=exception_statuses).count(),
        "trend": trend,
        "recent": recent,
    }


def audit_stream(limit: int = 25) -> list:
    """Recent platform-level audit events (provisioning, lifecycle,
    impersonation, go-live)."""
    from audit.models import AuditLog

    rows = (
        AuditLog.all_objects
        .filter(action__startswith="cooperative.")
        .select_related("cooperative")
        .order_by("-created_at")[:limit]
    )
    return [
        {
            "id": r.id,
            "action": r.action,
            "actor": r.actor_label or (
                r.actor.full_name if r.actor_id else "system"),
            "cooperative": (r.cooperative.name if r.cooperative_id else None),
            "entity": r.entity_repr,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


def provider_history(samples: int = 20) -> list:
    """Per-provider latency/status history for sparklines."""
    from platform_admin.models import ProviderStatus

    out = []
    for p in ProviderStatus.objects.all():
        checks = list(
            p.checks.order_by("-checked_at")[:samples].values(
                "latency_ms", "status", "checked_at"))
        checks.reverse()
        out.append({
            "id": p.id, "name": p.name, "kind": p.kind, "status": p.status,
            "latency_ms": p.latency_ms,
            "history": [
                {"latency_ms": c["latency_ms"], "status": c["status"],
                 "checked_at": c["checked_at"].isoformat()}
                for c in checks
            ],
        })
    return out


def health() -> dict:
    """Provider registry + system metrics computed from real payment data."""
    from platform_admin.models import ProviderStatus
    from platform_admin.serializers import ProviderStatusSerializer

    exception_statuses = [
        PaymentEvent.Status.UNMATCHED,
        PaymentEvent.Status.PARTIAL,
        PaymentEvent.Status.DUPLICATE,
    ]
    events = PaymentEvent.all_objects.all()
    total_events = events.count()
    matched = events.filter(status=PaymentEvent.Status.MATCHED).count()
    exceptions = events.filter(status__in=exception_statuses).count()
    webhook_success = round(matched / total_events * 100, 1) if total_events \
        else 100.0

    providers = ProviderStatusSerializer(
        ProviderStatus.objects.all(), many=True).data

    system = [
        {"label": "Webhook match rate",
         "value": f"{webhook_success}%",
         "foot": "matched / total events"},
        {"label": "Recon exceptions",
         "value": str(exceptions),
         "foot": f"across {Cooperative.objects.count()} tenants"},
        {"label": "Payment events",
         "value": str(total_events),
         "foot": "lifetime, all tenants"},
        {"label": "Providers degraded",
         "value": str(len(_degraded_providers())),
         "foot": "non-operational"},
    ]
    return {"providers": providers, "system": system}
