"""Put every live society on a plan, and make every member cap the plan's.

A society with no subscription had no member cap but the model's old default,
5,000 — so societies activated by the self-serve migration (and any provisioned
before billing reached them) showed a 5,000-member limit no plan carries. From
now on provisioning always creates the subscription; this catches up the rest.

Each gets a free trial starting today on the plan whose band holds its active
members — Starter for any small society, which is every new one. A society
above every band is left unbilled rather than guessed at (as
``backfill_subscriptions`` does), and its cap reads 0: no limit.

The plan choice repeats ``platform_admin.billing.plan_for`` on purpose: a
migration must not import live code, which may have changed by the time it
runs.
"""
from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Cooperative = apps.get_model("tenants", "Cooperative")
    Membership = apps.get_model("accounts", "Membership")
    Plan = apps.get_model("platform_admin", "Plan")
    Subscription = apps.get_model("platform_admin", "Subscription")

    plans = list(Plan.objects.filter(active=True)
                 .order_by("min_members", "price_monthly"))
    today = timezone.localdate()
    trial_days = getattr(settings, "BILLING_TRIAL_DAYS", 30)

    def plan_for(coop):
        if not plans:
            return None
        members = Membership.objects.filter(
            cooperative=coop, status="active").count()
        if members < plans[0].min_members:
            return plans[0]
        return next((p for p in plans if members >= p.min_members and (
            not p.max_members or members <= p.max_members)), None)

    billed = set(Subscription.objects.values_list("cooperative_id", flat=True))
    for coop in Cooperative.objects.filter(status="active").exclude(
            pk__in=billed):
        plan = plan_for(coop)
        if plan is None:
            continue
        Subscription.objects.create(
            cooperative=coop, plan=plan, status="trial",
            billing_cycle="monthly", monthly_value=plan.price_monthly,
            started_at=today, current_period_start=today,
            current_period_end=today + timedelta(days=trial_days),
        )

    caps = dict(Subscription.objects.values_list(
        "cooperative_id", "plan__max_members"))
    for coop in Cooperative.objects.all():
        cap = caps.get(coop.pk) or 0
        if coop.member_cap != cap:
            Cooperative.objects.filter(pk=coop.pk).update(member_cap=cap)


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0031_self_serve_signup_copy"),
        ("tenants", "0011_member_cap_follows_plan"),
        ("accounts", "0011_existing_passwords_confirm_email"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
