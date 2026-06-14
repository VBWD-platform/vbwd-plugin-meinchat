"""S86.3 — migration up/down/up for `meinchat_guest_session` (real PG).

Runs the migration through alembic's Operations context in a rolled-back
transaction. The conftest ``create_all`` already created the table, so it is
dropped first to exercise a clean upgrade. Validates the chain anchors on the
meinchat head `20260613_1000_mc_rooms` (NOT a foreign plugin) and the rev id is
≤ 32 chars.
"""
import importlib.util
import os

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect


def _load_migration():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "versions",
        "20260613_1100_mc_guest_session.py",
    )
    spec = importlib.util.spec_from_file_location("meinchat_guest_session", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
TABLE = "meinchat_guest_session"


def _has_table(connection, table) -> bool:
    return inspect(connection).has_table(table)


@pytest.fixture
def migration_connection(app):
    from vbwd.extensions import db

    connection = db.engine.connect()
    transaction = connection.begin()
    operations = Operations(MigrationContext.configure(connection))
    if _has_table(connection, TABLE):
        operations.drop_table(TABLE)
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.integration
def test_revision_anchors_on_meinchat_head_and_id_is_short():
    assert migration.revision == "20260613_1100_mc_guest_sess"
    assert migration.down_revision == "20260613_1000_mc_rooms"
    assert len(migration.revision) <= 32


@pytest.mark.integration
def test_up_down_up(migration_connection):
    connection = migration_connection
    assert not _has_table(connection, TABLE)

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.upgrade()
    assert _has_table(connection, TABLE)

    with Operations.context(context):
        migration.downgrade()
    assert not _has_table(connection, TABLE)

    with Operations.context(context):
        migration.upgrade()
    assert _has_table(connection, TABLE)
