"""
Seed a demo tenant so the API is explorable straight after ``migrate``.

Creates:
  * a platform super-admin (admin@startupripple.co / password: adminpass)
  * the "Ìmọ̀lè Multipurpose CS" cooperative from the design prototype
  * a Secretary login (secretary@imole.coop / password: coop1234)
  * a couple of members, contribution types, and recorded contributions

Idempotent-ish: safe to run on a fresh database. Run with:
    python manage.py seed_demo
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts.models import Membership, Role, User
from contributions.models import ContributionType
from contributions.services import record_contribution
from core.context import use_tenant
from ledger.models import Account
from tenants.models import Cooperative
from tenants.services import provision_cooperative


def seed_member_experience(coop):
    """Create a plain *member* login (member@imole.coop / member123) with data
    across every member-facing feature — savings, a savings goal, an active
    loan and a dividend — so the member PWA (/app) and mobile app can be tested
    end-to-end. Idempotent."""
    from decimal import Decimal

    from contributions.models import ContributionType
    from contributions.services import record_contribution
    from dividends.services import declare_dividend, post_dividend
    from loans.models import Loan, LoanProduct
    from loans.services import approve_loan, disburse_loan
    from savings.models import SavingsGoal, SavingsProduct

    with use_tenant(coop):
        if User.objects.filter(email="member@imole.coop").exists():
            return "member already seeded"

        member_role = Role.objects.filter(slug="member").first()
        dues = ContributionType.objects.filter(slug="monthly-dues").first()
        savings = ContributionType.objects.filter(slug="target-savings").first()
        officer = User.objects.filter(email="secretary@imole.coop").first()

        user = User.objects.create_user(
            email="member@imole.coop", password="member123",
            full_name="Ada Obi", phone="08055501234")
        member = Membership.objects.create(
            user=user, member_no="IMC-2001", role=member_role,
            share_capital="75000.00", bank_name="GTBank",
            bank_account_no="0123456789")

        # The society's own collection account (shown for direct-transfer
        # repayments). Idempotent guard already returned above if re-run.
        if not coop.bank_account_no:
            coop.bank_name = "Access Bank"
            coop.bank_account_name = coop.name
            coop.bank_account_no = "0987654321"
            coop.save(update_fields=["bank_name", "bank_account_name",
                                     "bank_account_no", "updated_at"])

        # Savings history (gives the member a real balance + statement lines).
        if dues:
            record_contribution(cooperative=coop, membership=member,
                                contribution_type=dues, amount="5000",
                                channel="cash", recorded_by=officer)
        if savings:
            record_contribution(cooperative=coop, membership=member,
                                contribution_type=savings, amount="40000",
                                channel="transfer", recorded_by=officer)

        # A savings product + a goal to track.
        product = SavingsProduct.objects.create(
            name="Target Savings (Ajo)", interest_rate="5",
            contribution_type=savings)
        SavingsGoal.objects.create(membership=member, product=product,
                                   name="New shop", target_amount="200000")

        # A live loan: apply → approve → disburse.
        loan_product = LoanProduct.objects.create(
            name="Quick Loan", interest_rate="10", max_amount="500000",
            max_term_months=12)
        loan = Loan.objects.create(
            membership=member, product=loan_product,
            principal=Decimal("100000"), interest_rate=Decimal("10"),
            term_months=6, purpose="Shop restock")
        approve_loan(loan, actor=officer, approve=True)
        disburse_loan(loan, actor=officer)

        # A posted dividend so the member sees an allocation.
        declaration = declare_dividend(
            cooperative=coop, total_amount="50000", period_label="FY2025",
            created_by=officer)
        post_dividend(declaration, actor=officer)

    return "member seeded"


class Command(BaseCommand):
    help = "Seed a demo cooperative with members and contributions."

    def handle(self, *args, **options):
        if Cooperative.objects.filter(slug="imole").exists():
            self.stdout.write(self.style.WARNING("Demo already seeded."))
            return

        User.objects.create_superuser(
            email="admin@startupripple.co", password="adminpass",
            full_name="Platform Admin",
        )

        coop = provision_cooperative(
            name="Ìmọ̀lè Multipurpose CS", slug="imole",
            state="Lagos", coop_type="multipurpose",
            tier=Cooperative.Tier.MEDIUM, member_cap=5000,
        )

        with use_tenant(coop):
            from payments.models import ProviderAccount

            funds = Account.objects.get(code="2000")
            roles = Role.seed_defaults(coop)

            ProviderAccount.objects.create(
                provider="paystack", subaccount_code="ACCT_8821",
                bank_name="GTBank",
            )

            dues = ContributionType.objects.create(
                name="Monthly Dues", slug="monthly-dues",
                frequency=ContributionType.Frequency.MONTHLY,
                kind=ContributionType.Kind.MANDATORY,
                expected_amount="5000.00", gl_account=funds,
            )
            savings = ContributionType.objects.create(
                name="Target Savings (Ajo)", slug="target-savings",
                frequency=ContributionType.Frequency.FLEXIBLE,
                kind=ContributionType.Kind.VOLUNTARY, gl_account=funds,
            )

            secretary = User.objects.create_user(
                email="secretary@imole.coop", password="coop1234",
                full_name="Blessing Okafor", phone="08030001111",
            )
            Membership.objects.create(
                user=secretary, member_no="IMC-0001",
                role=roles["secretary"], share_capital="100000.00",
            )

            adebayo_user = User.objects.create_user(
                email="adebayo@example.com", full_name="Adebayo Okonkwo",
                phone="08034412290",
            )
            adebayo = Membership.objects.create(
                user=adebayo_user, member_no="IMC-0847",
                role=roles["chairperson"], share_capital="250000.00",
            )

            # A little history so statements/dashboards have data.
            record_contribution(cooperative=coop, membership=adebayo,
                                contribution_type=dues, amount="5000",
                                channel="psp", psp_reference="PSTK_001",
                                recorded_by=secretary)
            record_contribution(cooperative=coop, membership=adebayo,
                                contribution_type=savings, amount="30000",
                                channel="transfer", recorded_by=secretary)

            # Governance: an AGM with an open resolution.
            from django.utils import timezone

            from communications.models import Announcement
            from communications.services import broadcast
            from governance.models import Meeting, Resolution
            from governance.services import open_resolution

            meeting = Meeting.objects.create(
                title="AGM 2026 — Civic Centre, Lagos & online",
                location="Civic Centre, Lagos",
                scheduled_at=timezone.now() + timezone.timedelta(days=11),
                proxy_enabled=True, status=Meeting.Status.SCHEDULED,
            )
            resolution = Resolution.objects.create(
                meeting=meeting, title="Increase monthly dues to ₦6,000",
                description="Approve raising monthly dues from ₦5,000.",
                created_by=secretary,
            )
            open_resolution(resolution, actor=secretary)

            # A sent announcement (fans out in-app + email).
            announcement = Announcement.objects.create(
                title="AGM 2026 — date confirmed & agenda",
                body=("Our Annual General Meeting holds on 12 July at the "
                      "Civic Centre and online. Voting opens at 09:00."),
                channels=["in_app", "email"], created_by=secretary,
            )
            broadcast(announcement)

        # Populate the Startup Ripple platform-ops surface (plans, billing,
        # onboarding, domains, provider health, team) plus a few more coops.
        from platform_admin.management.commands.seed_platform import (
            seed_platform_data,
        )
        platform_summary = seed_platform_data()

        # A plain member with data across every member-facing feature.
        seed_member_experience(coop)

        self.stdout.write(self.style.SUCCESS(
            "Seeded demo cooperative 'Ìmọ̀lè Multipurpose CS'.\n"
            f"  Platform-ops:   {platform_summary}\n"
            "  Platform admin: admin@startupripple.co / adminpass\n"
            "  Secretary:      secretary@imole.coop / coop1234\n"
            "  Member (app):   member@imole.coop / member123"
        ))
