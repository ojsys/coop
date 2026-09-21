"""Shared fixtures for the CooperativeOS test suite."""
from __future__ import annotations

import pytest

from accounts.models import Membership, Role, User
from contributions.models import ContributionType
from core.context import use_tenant
from ledger.models import Account
from tenants.services import provision_cooperative


@pytest.fixture
def coop(db):
    """A fully provisioned cooperative (chart of accounts + roles seeded)."""
    return provision_cooperative(
        name="Ìmọ̀lè Multipurpose CS", slug="imole",
        state="Lagos", coop_type="multipurpose",
    )


@pytest.fixture
def other_coop(db):
    return provision_cooperative(name="Aba Traders Cooperative", slug="aba")


@pytest.fixture
def member_funds(coop):
    return Account.all_objects.get(cooperative=coop, code="2000")


@pytest.fixture
def dues_type(coop, member_funds):
    """A monthly-dues contribution type crediting Member Funds."""
    with use_tenant(coop):
        return ContributionType.objects.create(
            name="Monthly Dues", slug="monthly-dues",
            frequency=ContributionType.Frequency.MONTHLY,
            kind=ContributionType.Kind.MANDATORY,
            expected_amount="5000.00", gl_account=member_funds,
        )


@pytest.fixture
def member(coop):
    """An active member of ``coop``."""
    with use_tenant(coop):
        roles = Role.objects.all()  # seeded
        user = User.objects.create_user(
            email="adebayo@example.com", full_name="Adebayo Okonkwo",
            phone="08034412290",
        )
        return Membership.objects.create(
            user=user, member_no="IMC-0847", share_capital="250000.00",
            role=roles.filter(slug="member").first(),
        )


@pytest.fixture
def make_member(coop):
    """Factory to create additional active members of ``coop``."""
    counter = {"n": 0}

    def _make(member_no=None, share_capital="0",
              status=Membership.Status.ACTIVE, phone=""):
        counter["n"] += 1
        n = counter["n"]
        with use_tenant(coop):
            user = User.objects.create_user(
                email=f"member{n}@example.com", full_name=f"Member {n}",
                phone=phone or f"0803000{n:04d}",
            )
            return Membership.objects.create(
                user=user, member_no=member_no or f"IMC-{1000 + n}",
                share_capital=share_capital, status=status,
            )

    return _make
