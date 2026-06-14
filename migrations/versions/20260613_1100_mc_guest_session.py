"""S86.3 — meinchat guest widget-session table (`meinchat_guest_session`).

One row per public bot-widget "Start Conversation" that provisions a core GUEST
user, linking guest user ↔ widget slug ↔ room. A later slice (D11/D12) builds
session reuse + the token grant on this row.

Guarded + idempotent (monolith / create_all / re-runs). Chains off the meinchat
head `20260613_1000_mc_rooms` — NOT a foreign plugin.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# rev id ≤ 32 chars.
revision = "20260613_1100_mc_guest_sess"
down_revision = "20260613_1000_mc_rooms"
branch_labels = None
depends_on = None

_TABLE = "meinchat_guest_session"


def _has_table(conn, table: str) -> bool:
    return sa.inspect(conn).has_table(table)


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "guest_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("widget_slug", sa.String(length=255), nullable=False),
        sa.Column(
            "room_id",
            UUID(as_uuid=True),
            sa.ForeignKey("meinchat_room.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("token_jti", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meinchat_guest_session_guest_user_id", _TABLE, ["guest_user_id"]
    )
    op.create_index("ix_meinchat_guest_session_widget_slug", _TABLE, ["widget_slug"])


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    op.drop_index("ix_meinchat_guest_session_widget_slug", table_name=_TABLE)
    op.drop_index("ix_meinchat_guest_session_guest_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
