"""Install the member-band price list and move every subscription onto it.

From the October 2026 pricing audit. The previous plans (Basic / Pro / Premium,
or Starter / Growth / Institutional on seeded databases) used size labels that
contradicted their own member ranges. They are deactivated, not deleted —
invoices and history still point at them — and every subscription is moved to
the plan whose band holds its cooperative's active member count, at the new
price, from its next invoice. Invoices already raised keep their amounts.
"""
from decimal import Decimal

from django.db import migrations

PLANS = [
    # name, tier, monthly, annual (None = quoted), min, max (0 = no cap), from?
    ("Starter", "small", "9000", "90000", 50, 250, False),
    ("Growth", "medium", "15000", "150000", 251, 1000, False),
    ("Professional", "large", "29000", "290000", 1001, 5000, False),
    ("Institutional", "large", "49000", None, 5001, 0, True),
]


def forwards(apps, schema_editor):
    Plan = apps.get_model("platform_admin", "Plan")
    Subscription = apps.get_model("platform_admin", "Subscription")
    Membership = apps.get_model("accounts", "Membership")

    # This replaces a price list. A database without one — a fresh install or
    # the test database — has nothing to replace; seed_platform provides the
    # same plans there.
    if not Plan.objects.exists():
        return

    keep = []
    for name, tier, monthly, annual, lo, hi, is_from in PLANS:
        plan = Plan.objects.filter(name=name).order_by("id").first() or Plan(
            name=name)
        plan.tier = tier
        plan.price_monthly = Decimal(monthly)
        plan.price_annual = Decimal(annual) if annual else None
        plan.price_is_from = is_from
        plan.min_members = lo
        plan.max_members = hi
        # Left blank: the band already says who the plan is for, and what is
        # included is for the business to confirm before it is published.
        plan.description = ""
        plan.active = True
        plan.save()
        keep.append(plan)

    Plan.objects.exclude(pk__in=[p.pk for p in keep]).update(active=False)

    def band_for(members):
        for plan in keep:
            if members >= plan.min_members and (
                    not plan.max_members or members <= plan.max_members):
                return plan
        # Below the smallest band: the entry plan.
        return keep[0]

    for sub in Subscription.objects.all():
        members = Membership.objects.filter(
            cooperative_id=sub.cooperative_id, status="active").count()
        plan = band_for(members)
        sub.plan = plan
        amount = (sub.price_override if sub.price_override is not None
                  else plan.price_annual if sub.billing_cycle == "annual"
                  else plan.price_monthly)
        if amount is None:
            sub.monthly_value = Decimal("0.00")
        elif sub.billing_cycle == "annual":
            sub.monthly_value = (amount / 12).quantize(Decimal("0.01"))
        else:
            sub.monthly_value = amount
        sub.save(update_fields=["plan", "monthly_value", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0024_annual_billing_and_bands"),
        ("accounts", "0004_membership_bank_code"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
