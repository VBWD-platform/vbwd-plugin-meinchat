"""S43.0b — prefix the remaining bare meinchat tables: conversation/message.

`conversation` → `meinchat_conversation`, `message` → `meinchat_message`.
Internal FKs (message→conversation, meinchat_attachment→message) auto-follow the
rename in Postgres; the cross-plugin `meinchat_plus_message_delivery→message` FK
ALSO auto-follows (the meinchat_plus model FK string is updated in lockstep, and
its creation migration resolves the table name dynamically so a fresh-DB alembic
run works in either branch order).

PRESERVES DATA: pure `ALTER TABLE … RENAME` (+ dependent renames), no
drop/recreate. Runs on PROD via `deploy.sh --migrate` in CI: guarded +
idempotent (monolith/create_all/re-runs).
"""
import sqlalchemy as sa
from alembic import op

revision = "20260531_meinchat_prefix_b"
down_revision = "20260531_meinchat_prefix_a"
branch_labels = None
depends_on = None

_RENAMES = {
    "conversation": "meinchat_conversation",
    "message": "meinchat_message",
}


def _table_exists(conn, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def _rename_dependents(conn, table: str, frm: str, to: str) -> None:
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
            op.execute(
                f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" '
                f'TO "{name.replace(frm, to, 1)}"'
            )
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
