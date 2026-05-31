"""S43.0a — prefix the bare meinchat table names with `meinchat_`.

`token_transfer` / `user_contact` / `user_nickname` had unprefixed names
(audit report 02). Renames the tables AND their dependent constraints +
indexes (Postgres does NOT auto-rename those on `ALTER TABLE … RENAME`).

PRESERVES DATA: pure `ALTER TABLE … RENAME` (+ rename of constraints/indexes) —
no drop/recreate, every row is kept. Runs on PROD via `deploy.sh --migrate`
(`alembic upgrade heads`) in the GitHub CI, so it is guarded + idempotent: safe
on a monolith-built prod DB, a `create_all` dev DB, and re-runs. Dependent
objects are renamed by introspection + substring swap, so it works regardless of
whether they were created by SQLAlchemy autogen or by an explicit-name migration.
These three tables have **no** foreign keys pointing at them, so the rename is
self-contained (sprint S43).
"""
import sqlalchemy as sa
from alembic import op

revision = "20260531_meinchat_prefix_a"
down_revision = "20260603_1000_drop_msg_attach_cols"
branch_labels = None
depends_on = None

_RENAMES = {
    "token_transfer": "meinchat_token_transfer",
    "user_contact": "meinchat_user_contact",
    "user_nickname": "meinchat_user_nickname",
}


def _table_exists(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def _rename_dependents(conn, table: str, frm: str, to: str) -> None:
    """Rename every constraint + plain index on `table` whose name contains
    `frm`, swapping the first occurrence for `to`.

    Constraints (pkey/unique/check/fk) are renamed via ALTER TABLE, which also
    renames their backing index — so the index loop EXCLUDES constraint-backed
    indexes (by oid) to avoid double-renaming the pkey's index.
    """
    constraints = (
        conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:t)"
            ),
            {"t": table},
        )
        .scalars()
        .all()
    )
    for name in constraints:
        if frm in name:
            new = name.replace(frm, to, 1)
            op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" TO "{new}"')
    plain_indexes = (
        conn.execute(
            sa.text(
                "SELECT i.relname FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "WHERE x.indrelid = to_regclass(:t) "
                "AND x.indexrelid NOT IN "
                "(SELECT conindid FROM pg_constraint WHERE conindid <> 0)"
            ),
            {"t": table},
        )
        .scalars()
        .all()
    )
    for name in plain_indexes:
        if frm in name:
            op.execute(f'ALTER INDEX "{name}" RENAME TO "{name.replace(frm, to, 1)}"')


def upgrade() -> None:
    conn = op.get_bind()
    for old, new in _RENAMES.items():
        if _table_exists(conn, old) and not _table_exists(conn, new):
            op.rename_table(old, new)
            _rename_dependents(conn, new, old, new)


def downgrade() -> None:
    conn = op.get_bind()
    for old, new in _RENAMES.items():
        if _table_exists(conn, new) and not _table_exists(conn, old):
            _rename_dependents(conn, new, new, old)
            op.rename_table(new, old)
