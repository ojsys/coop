"""Tenant-isolation tests — row-level scoping must never leak (PRD §11)."""
from __future__ import annotations

import pytest

from accounts.models import Membership, Role, User
from contributions.models import ContributionType
from core.context import get_current_cooperative, use_tenant
from ledger.models import Account

pytestmark = pytest.mark.django_db


def _make_member(coop, email, member_no):
    with use_tenant(coop):
        user = User.objects.create_user(email=email, full_name=email)
        return Membership.objects.create(user=user, member_no=member_no)


def test_scoped_manager_returns_nothing_without_tenant(coop):
    _make_member(coop, "a@x.com", "M1")
    # No tenant bound → fail closed.
    assert get_current_cooperative() is None
    assert Membership.objects.count() == 0


def test_members_isolated_between_cooperatives(coop, other_coop):
    _make_member(coop, "a@x.com", "M1")
    _make_member(other_coop, "b@x.com", "M1")  # same member_no, different coop

    with use_tenant(coop):
        emails = set(Membership.objects.values_list("user__email", flat=True))
        assert emails == {"a@x.com"}

    with use_tenant(other_coop):
        emails = set(Membership.objects.values_list("user__email", flat=True))
        assert emails == {"b@x.com"}


def test_accounts_and_types_isolated(coop, other_coop):
    with use_tenant(coop):
        assert Account.objects.filter(code="2000").exists()
    with use_tenant(other_coop):
        # other_coop has its own seeded 2000, not coop's.
        acct = Account.objects.get(code="2000")
        assert acct.cooperative_id == other_coop.id


def test_roles_seeded_per_cooperative(coop, other_coop):
    with use_tenant(coop):
        assert Role.objects.count() == len(Role.DEFAULT_ROLES)
    with use_tenant(other_coop):
        assert Role.objects.count() == len(Role.DEFAULT_ROLES)


def test_cross_tenant_write_is_prevented_by_save_scoping(coop, other_coop):
    """A type created under one tenant context belongs to that tenant."""
    funds = Account.all_objects.get(cooperative=coop, code="2000")
    with use_tenant(coop):
        ct = ContributionType.objects.create(
            name="Building Fund", slug="building-fund", gl_account=funds,
        )
    assert ct.cooperative_id == coop.id
    with use_tenant(other_coop):
        assert not ContributionType.objects.filter(slug="building-fund").exists()
