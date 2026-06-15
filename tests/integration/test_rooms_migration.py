"""S86.1 — migration up/down/up for the rooms tables + message.room_id (real PG).

Loads the migration module directly and runs it through alembic's Operations
context in a rolled-back transaction (the shared test DB is left untouched). The
conftest ``create_all`` already created the room tables / column, so they are
dropped first to exercise a clean upgrade. Validates the chain anchors on the
meinchat head (NOT a foreign plugin) and the rev id is ≤ 32 chars.
"""
import importlib.util
import os

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect


# This migration spec opens its OWN connection + transaction and rolls back
# itself, so it must run WITHOUT the autouse rolled-back-session isolation
# (which swaps ``db.engine`` for a Connection). See conftest ``no_db_isolation``.
pytestmark = pytest.mark.no_db_isolation


def _load_migration():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "versions",
        "20260613_1000_meinchat_rooms.py",
    )
    spec = importlib.util.spec_from_file_location("meinchat_rooms", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
ROOM = "meinchat_room"
ROOM_MEMBER = "meinchat_room_member"
MESSAGE = "meinchat_message"
ONE_PARENT_CHECK = "ck_meinchat_message_one_parent"


def _has_table(connection, table) -> bool:
    return inspect(connection).has_table(table)


def _has_column(connection, table, column) -> bool:
    if not _has_table(connection, table):
        return False
    return column in {c["name"] for c in inspect(connection).get_columns(table)}


def _has_check(connection, table, name) -> bool:
    return name in {c["name"] for c in inspect(connection).get_check_constraints(table)}


@pytest.fixture
def migration_connection(app):
    from vbwd.extensions import db

    connection = db.engine.connect()
    transaction = connection.begin()
    operations = Operations(MigrationContext.configure(connection))
    # Strip what create_all() built so the upgrade runs against a clean state.
    if _has_check(connection, MESSAGE, ONE_PARENT_CHECK):
        operations.drop_constraint(ONE_PARENT_CHECK, MESSAGE, type_="check")
    if _has_column(connection, MESSAGE, "room_id"):
        operations.drop_index("ix_meinchat_message_room_id", table_name=MESSAGE)
        operations.drop_column(MESSAGE, "room_id")
    operations.alter_column(MESSAGE, "conversation_id", nullable=False)
    if _has_table(connection, ROOM_MEMBER):
        operations.drop_table(ROOM_MEMBER)
    # S86.3 added meinchat_guest_session with an FK to meinchat_room; create_all
    # builds it, so drop it before the room table or the DROP is blocked.
    if _has_table(connection, "meinchat_guest_session"):
        operations.drop_table("meinchat_guest_session")
    if _has_table(connection, ROOM):
        operations.drop_table(ROOM)
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.integration
def test_revision_anchors_on_meinchat_head_and_id_is_short():
    assert migration.revision == "20260613_1000_mc_rooms"
    assert migration.down_revision == "20260611_1000_mc_msg_meta"
    assert len(migration.revision) <= 32


@pytest.mark.integration
def test_up_down_up(migration_connection):
    connection = migration_connection
    assert not _has_table(connection, ROOM)
    assert not _has_table(connection, ROOM_MEMBER)
    assert not _has_column(connection, MESSAGE, "room_id")

    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.upgrade()
    assert _has_table(connection, ROOM)
    assert _has_table(connection, ROOM_MEMBER)
    assert _has_column(connection, MESSAGE, "room_id")
    assert _has_check(connection, MESSAGE, ONE_PARENT_CHECK)

    with Operations.context(context):
        migration.downgrade()
    assert not _has_table(connection, ROOM)
    assert not _has_table(connection, ROOM_MEMBER)
    assert not _has_column(connection, MESSAGE, "room_id")
    assert not _has_check(connection, MESSAGE, ONE_PARENT_CHECK)

    with Operations.context(context):
        migration.upgrade()
    assert _has_table(connection, ROOM)
    assert _has_table(connection, ROOM_MEMBER)
    assert _has_column(connection, MESSAGE, "room_id")
