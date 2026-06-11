"""S70.0 — migration up/down/up for the meinchat_message.meta column (real PG).

Loads the migration module directly and runs it through alembic's Operations
context in a rolled-back transaction (the shared test DB is left untouched). The
conftest ``create_all`` already added the column, so it is dropped first to
exercise a clean upgrade. Validates the chain anchors on the prior meinchat head
and the rev id is ≤ 32 chars.
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
        "20260611_1000_meinchat_message_meta.py",
    )
    spec = importlib.util.spec_from_file_location("meinchat_message_meta", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
TABLE = "meinchat_message"
COLUMN = "meta"


def _has_column(connection) -> bool:
    if not inspect(connection).has_table(TABLE):
        return False
    return COLUMN in {c["name"] for c in inspect(connection).get_columns(TABLE)}


@pytest.fixture
def migration_connection(app):
    from vbwd.extensions import db

    connection = db.engine.connect()
    transaction = connection.begin()
    operations = Operations(MigrationContext.configure(connection))
    if _has_column(connection):
        operations.drop_column(TABLE, COLUMN)
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.integration
def test_revision_anchors_on_prior_meinchat_head_and_id_is_short():
    assert migration.revision == "20260611_1000_mc_msg_meta"
    assert migration.down_revision == "20260531_meinchat_prefix_b"
    assert len(migration.revision) <= 32


@pytest.mark.integration
def test_up_down_up(migration_connection):
    assert not _has_column(migration_connection)
    context = MigrationContext.configure(migration_connection)
    with Operations.context(context):
        migration.upgrade()
    assert _has_column(migration_connection)
    with Operations.context(context):
        migration.downgrade()
    assert not _has_column(migration_connection)
    with Operations.context(context):
        migration.upgrade()
    assert _has_column(migration_connection)
