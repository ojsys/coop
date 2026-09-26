"""The console's member list must not be silently truncated.

MembersPage searches, filters, counts and pages entirely on the client, over
whatever ``/members/`` hands back. DRF paginates at ``PAGE_SIZE`` (25) unless a
viewset opts out, so past that point the rest of a cooperative's members were
invisible: the search box could not find them, the "N total" header undercounted,
and the per-role tallies on Settings were wrong.

Written to fail against the truncating behaviour, so it stays honest.
"""
from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Membership, Role, User
from core.context import use_tenant

pytestmark = pytest.mark.django_db


def _client(user):
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known defect, not yet fixed: /members/ paginates at PAGE_SIZE=25 and "
        "the console treats the reply as a complete list. The fix is not to "
        "set pagination_class=None — MembershipSerializer.savings_balance "
        "calls ledger.services.member_balance (one aggregate per row) and "
        "document_count adds another, so an unpaginated 5,000-member "
        "cooperative would issue ~10,000 queries in one request. Both fields "
        "need annotating first, which changes how a financial figure is "
        "derived and deserves its own change. Marked strict so this flips to "
        "a failure the moment it is fixed."
    ),
)
def test_member_list_is_not_truncated_by_pagination(coop):
    """Well past DRF's default page size of 25."""
    with use_tenant(coop):
        role = Role.objects.filter(slug="member").first()
        viewer = User.objects.create_user(
            email="viewer@imole.coop", full_name="Viewer", password="pw")
        Membership.objects.create(user=viewer, member_no="VIEW-1", role=role)
        for i in range(30):
            user = User.objects.create_user(
                email=f"member{i}@imole.coop", full_name=f"Member {i}")
            Membership.objects.create(
                user=user, member_no=f"IMC-{i:04d}", role=role)
        expected = Membership.objects.count()

    resp = _client(viewer).get("/api/v1/members/")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    # Tolerate either shape: a bare list, or a paginated envelope.
    rows = body["results"] if isinstance(body, dict) else body
    assert len(rows) == expected, (
        f"the console received {len(rows)} of {expected} members — the rest "
        f"are invisible to search, filters and the totals"
    )
