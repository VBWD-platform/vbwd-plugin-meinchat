"""S70.0 — add nullable `meta` JSON to meinchat_message.

Structured/interactive content (bot choice cards / a tapped card's action)
rides alongside the plain `body`. Additive + nullable: every existing row keeps
`meta` NULL and renders exactly as before (Liskov — non-`meta` clients see only
`body`). e2e (`envelope`) rows are unaffected — rich choices ride the plain
path only. Guarded + idempotent (monolith / create_all / re-runs).
"""
import sqlalchemy as sa
from alembic import op

# rev id ≤ 32 chars.
revision = "20260611_1000_mc_msg_meta"
down_revision = "20260531_meinchat_prefix_b"
branch_labels = None
depends_on = None

_TABLE = "meinchat_message"
_COLUMN = "meta"


def _has_column(conn, table: str, column: str) -> bool:
    if not sa.inspect(conn).has_table(table):
        return False
    return column in {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, _TABLE, _COLUMN):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn, _TABLE, _COLUMN):
        op.drop_column(_TABLE, _COLUMN)
