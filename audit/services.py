"""Recording audit entries. One entry point so every logged action is uniform."""
from __future__ import annotations

from audit.models import AuditLog


def record_action(
    *,
    cooperative=None,
    actor=None,
    action: str,
    entity=None,
    entity_type: str = "",
    entity_id: str = "",
    entity_repr: str = "",
    before=None,
    after=None,
    actor_label: str = "",
) -> AuditLog:
    """Write an immutable audit entry.

    ``entity`` may be a model instance (its type/id/repr are derived) or you can
    pass ``entity_type``/``entity_id``/``entity_repr`` explicitly.
    """
    if entity is not None:
        entity_type = entity_type or type(entity).__name__
        entity_id = entity_id or str(getattr(entity, "pk", ""))
        entity_repr = entity_repr or str(entity)[:255]
        if cooperative is None:
            cooperative = getattr(entity, "cooperative", None)

    return AuditLog.all_objects.create(
        cooperative=cooperative,
        actor=actor if getattr(actor, "pk", None) else None,
        actor_label=actor_label or ("" if getattr(actor, "pk", None) else "System"),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_repr=entity_repr,
        before=before,
        after=after,
    )
