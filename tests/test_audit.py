"""Immutable audit trail (PRD §6.5)."""
from __future__ import annotations

import pytest

from audit.models import AuditLog
from audit.services import record_action
from contributions.services import record_contribution, reverse_contribution
from core.context import use_tenant
from core.models import ImmutableRecordError

pytestmark = pytest.mark.django_db


def test_record_action_creates_entry(coop, member):
    entry = record_action(
        cooperative=coop, action="Suspended member",
        entity=member, after={"status": "suspended"},
    )
    assert entry.pk is not None
    assert entry.entity_type == "Membership"
    assert entry.actor_label == "System"


def test_audit_entries_are_immutable(coop):
    entry = record_action(cooperative=coop, action="Something happened")
    entry.action = "tampered"
    with pytest.raises(ImmutableRecordError):
        entry.save()
    with pytest.raises(ImmutableRecordError):
        entry.delete()


def test_reversal_writes_audit_entry(coop, member, dues_type):
    with use_tenant(coop):
        contribution = record_contribution(
            cooperative=coop, membership=member, contribution_type=dues_type,
            amount="5000", channel="cash",
        )
        reverse_contribution(contribution, memo="duplicate")
        entries = AuditLog.objects.filter(action__startswith="Posted reversing")
        assert entries.count() == 1
