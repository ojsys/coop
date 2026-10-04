"""
Every model change must have a migration.

A forgotten ``makemigrations`` does not fail locally: ``ordering`` and other
Meta options are applied in Python, and a new field only breaks once something
reads the column. So the suite stays green while the deployed database is a
schema behind — and on this project a deploy is a file upload with no rollback
(see DEPLOY_CPANEL.md), which makes "discovered in production" expensive.

This is the cheapest possible guard: it asks Django the same question
``makemigrations --check`` does, inside the suite that already runs.
"""
from __future__ import annotations

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import ProjectState

pytestmark = pytest.mark.django_db


def test_no_model_changes_are_missing_a_migration():
    loader = MigrationExecutor(connection).loader
    autodetector = MigrationAutodetector(
        loader.project_state(),
        ProjectState.from_apps(apps),
    )
    changes = autodetector.changes(graph=loader.graph)

    described = {
        app: [
            f"{type(op).__name__}"
            + (f" {getattr(op, 'name', getattr(op, 'name_lower', ''))}"
               if hasattr(op, "name") or hasattr(op, "name_lower") else "")
            for migration in migrations
            for op in migration.operations
        ]
        for app, migrations in changes.items()
    }

    assert not changes, (
        "These model changes have no migration — run "
        "`python manage.py makemigrations`:\n"
        + "\n".join(f"  {app}: {', '.join(ops)}"
                    for app, ops in described.items())
    )
