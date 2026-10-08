"""
What a cooperative's subscription plan includes.

Every plan carries the full system of record — members, contributions, loans,
the ledger, statements, governance, auditor exports. That is the product's
promise ("every naira, traceable") and is never gated: a cheaper plan with a
weaker audit trail would undercut the very thing a society is buying.

Plans differ instead by what moves money for the society, how it is branded and
how far it reaches. Each such feature has a key here; ``Plan.features`` lists the
keys a plan includes, and every surface — API, console, member apps, the public
price card — reads that one list, so the website, the app and an agent's quote
cannot disagree.

Rules worth knowing before changing this:

* **No subscription means everything.** A society not yet billed (a pilot, or
  one provisioned before billing existed) must not lose features silently. The
  platform dashboard is where an unbilled society shows up.
* **The grace period means everything.** ``Subscription.features_grace_until``
  lets existing societies keep what they use while they decide on a plan.
* **A society's own money is never gated.** Withdrawing the disbursement wallet
  and members' savings withdrawals stay open on every plan; only *new* use of a
  feature is refused.
"""
from __future__ import annotations

from rest_framework.exceptions import PermissionDenied

ELECTRONIC_PAYOUTS = "electronic_payouts"
DIVIDENDS = "dividends"
SAVINGS_PLANS = "savings_plans"
CUSTOM_BRANDING = "custom_branding"
CUSTOM_DOMAIN = "custom_domain"

# key → (label for price cards and messages, one-line explanation)
CATALOGUE: dict[str, tuple[str, str]] = {
    ELECTRONIC_PAYOUTS: (
        "Electronic loan payouts",
        "Fund a disbursement wallet and pay approved loans straight to "
        "members' banks."),
    DIVIDENDS: (
        "Dividends",
        "Calculate the year's dividend and pay it to members."),
    SAVINGS_PLANS: (
        "Savings plans and goals",
        "Target savings products and member savings goals."),
    CUSTOM_BRANDING: (
        "Your society's logo and colours",
        "The member app, console and statements wear your brand."),
    CUSTOM_DOMAIN: (
        "Your own web address",
        "Members sign in at your society's domain (white label)."),
}

# Shown once, above the plans: what every plan includes. Words, not keys —
# these are never gated, so nothing checks them.
CORE: list[str] = [
    "Member register with KYC documents",
    "Contributions, dues and levies",
    "Online contribution payments through Paystack",
    "Loans with repayment schedules",
    "Append-only ledger and member statements",
    "Meetings, resolutions and voting",
    "Announcements to members",
    "Two-officer approval for money leaving",
    "Exports for your auditor",
    "Member app on web and Android",
]


class FeatureNotIncluded(PermissionDenied):
    """Raised (as HTTP 403) when the society's plan does not include a feature."""

    default_code = "plan_feature"


def subscription_for(cooperative):
    from platform_admin.models import Subscription

    return (Subscription.objects.select_related("plan")
            .filter(cooperative=cooperative).first())


def in_grace(subscription, *, today=None) -> bool:
    from django.utils import timezone

    until = subscription.features_grace_until if subscription else None
    return bool(until and until >= (today or timezone.localdate()))


def features_for(cooperative) -> set[str]:
    """The feature keys this cooperative may use right now."""
    if cooperative is None:
        return set(CATALOGUE)
    sub = subscription_for(cooperative)
    if sub is None or in_grace(sub):
        return set(CATALOGUE)
    return set(sub.plan.features or []) & set(CATALOGUE)


def has_feature(cooperative, key: str) -> bool:
    return key in features_for(cooperative)


def first_plan_with(key: str):
    """The smallest active plan that includes ``key`` — what to upgrade to."""
    from platform_admin.models import Plan

    for plan in Plan.objects.filter(active=True).order_by("min_members"):
        if key in (plan.features or []):
            return plan
    return None


def not_included_message(cooperative, key: str) -> str:
    label = CATALOGUE[key][0]
    sub = subscription_for(cooperative)
    upgrade = first_plan_with(key)
    current = f" {cooperative.name} is on {sub.plan.name}." if sub else ""
    if upgrade is None:
        return f"{label} is not included in your plan.{current}"
    return (f"{label} is included from the {upgrade.name} plan.{current} "
            f"Contact us to upgrade.")


def require_feature(cooperative, key: str) -> None:
    if not has_feature(cooperative, key):
        raise FeatureNotIncluded(not_included_message(cooperative, key))


class RequiresFeatureMixin:
    """Gate a viewset's writes on a plan feature.

    Only *new use* is refused. Reads stay open on every plan: past dividends
    and existing savings goals are history about members' own money, and a
    plan change must never make that history disappear.

    Checked in ``initial`` *after* the tenant is bound, which is why this is a
    mixin and not a DRF permission class: permissions run before
    ``TenantScopedViewMixin`` knows which cooperative the request is for.
    List it before ``TenantScopedViewMixin`` in the bases.
    """

    required_feature: str = ""
    # Writes that correct past use rather than start new use — a reversal, for
    # instance — and so must work on any plan.
    ungated_actions: frozenset = frozenset()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        if getattr(self, "action", None) in self.ungated_actions:
            return
        from core.context import get_current_cooperative

        require_feature(get_current_cooperative(), self.required_feature)
