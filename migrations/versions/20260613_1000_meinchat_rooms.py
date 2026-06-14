"""S86.1 — meinchat rooms: `meinchat_room` + `meinchat_room_member` tables and
an optional `meinchat_message.room_id` (D1/D2).

Creates the two room tables and gives `meinchat_message` a second, nullable
parent FK (`room_id`), relaxing `conversation_id` to nullable and adding a CHECK
that exactly one parent is set. Backward-compatible: every existing message keeps
`conversation_id` populated and `room_id` NULL, satisfying the new CHECK.

Guarded + idempotent (monolith / create_all / re-runs). Chains off the meinchat
head `20260611_1000_mc_msg_meta` — NOT a foreign plugin.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# rev id ≤ 32 chars.
revision = "20260613_1000_mc_rooms"
down_revision = "20260611_1000_mc_msg_meta"
branch_labels = None
depends_on = None

_ROOM = "meinchat_room"
_ROOM_MEMBER = "meinchat_room_member"
_MESSAGE = "meinchat_message"
_ONE_PARENT_CHECK = "ck_meinchat_message_one_parent"


def _has_table(conn, table: str) -> bool:
    return sa.inspect(conn).has_table(table)


def _has_column(conn, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    return column in {c["name"] for c in sa.inspect(conn).get_columns(table)}


def _has_check(conn, table: str, name: str) -> bool:
    if not _has_table(conn, table):
        return False
    return name in {c["name"] for c in sa.inspect(conn).get_check_constraints(table)}


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, _ROOM):
        op.create_table(
            _ROOM,
            sa.Column("id", UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=True),
            sa.Column(
                "created_by_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id"),
                nullable=False,
            ),
            sa.Column(
                "protocol",
                sa.String(length=32),
                nullable=False,
                server_default="plain",
            ),
            sa.Column(
                "capabilities",
                JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("widget_slug", sa.String(length=255), nullable=True),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_message_preview", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_meinchat_room_created_by_id", _ROOM, ["created_by_id"])

    if not _has_table(conn, _ROOM_MEMBER):
        op.create_table(
            _ROOM_MEMBER,
            sa.Column("id", UUID(as_uuid=True), nullable=False),
            sa.Column(
                "room_id",
                UUID(as_uuid=True),
                sa.ForeignKey("meinchat_room.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column(
                "invited_by_id",
                UUID(as_uuid=True),
                sa.ForeignKey("vbwd_user.id"),
                nullable=True,
            ),
            sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "unread_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "room_id", "user_id", name="uq_meinchat_room_member_room_user"
            ),
            sa.CheckConstraint(
                "role IN ('admin', 'member')",
                name="ck_meinchat_room_member_role",
            ),
        )
        op.create_index("ix_meinchat_room_member_room_id", _ROOM_MEMBER, ["room_id"])
        op.create_index("ix_meinchat_room_member_user_id", _ROOM_MEMBER, ["user_id"])

    # D2 — give meinchat_message an optional room parent.
    if not _has_column(conn, _MESSAGE, "room_id"):
        op.add_column(
            _MESSAGE,
            sa.Column(
                "room_id",
                UUID(as_uuid=True),
                sa.ForeignKey("meinchat_room.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index("ix_meinchat_message_room_id", _MESSAGE, ["room_id"])

    op.alter_column(
        _MESSAGE, "conversation_id", existing_type=UUID(as_uuid=True), nullable=True
    )

    if not _has_check(conn, _MESSAGE, _ONE_PARENT_CHECK):
        op.create_check_constraint(
            _ONE_PARENT_CHECK,
            _MESSAGE,
            "(conversation_id IS NULL) <> (room_id IS NULL)",
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_check(conn, _MESSAGE, _ONE_PARENT_CHECK):
        op.drop_constraint(_ONE_PARENT_CHECK, _MESSAGE, type_="check")

    # Restore NOT NULL on conversation_id. No room exists when downgrading
    # (the room tables are about to be dropped), so every message is a
    # conversation message and the restore cannot violate.
    if _has_column(conn, _MESSAGE, "room_id"):
        op.drop_index("ix_meinchat_message_room_id", table_name=_MESSAGE)
        op.drop_column(_MESSAGE, "room_id")
    op.alter_column(
        _MESSAGE, "conversation_id", existing_type=UUID(as_uuid=True), nullable=False
    )

    if _has_table(conn, _ROOM_MEMBER):
        op.drop_index("ix_meinchat_room_member_user_id", table_name=_ROOM_MEMBER)
        op.drop_index("ix_meinchat_room_member_room_id", table_name=_ROOM_MEMBER)
        op.drop_table(_ROOM_MEMBER)
    if _has_table(conn, _ROOM):
        op.drop_index("ix_meinchat_room_created_by_id", table_name=_ROOM)
        op.drop_table(_ROOM)
