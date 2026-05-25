"""meinchat: create user_nickname table.

Sprint 57 slice one — nickname subsystem. Subsequent slices add
user_contact, conversation + message, and token_transfer tables.

Revision ID: 20260424_1000
Revises: 20260523_1000_sub_baseline
Create Date: 2026-04-24

meinchat declares a dependency on the subscription plugin, so its migration
branch anchors on subscription's `sub_baseline` (not on the cms `20260422_1200`
it chained off purely by creation order). The declared dep makes subscription's
migrations available wherever meinchat's run (runtime + CI).
"""
from alembic import op
import sqlalchemy as sa


revision = "20260424_1000"
down_revision = "20260523_1000_sub_baseline"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if _table_exists(conn, "user_nickname"):
        return

    op.create_table(
        "user_nickname",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nickname", sa.String(32), nullable=False),
        sa.Column(
            "nickname_ci",
            sa.String(32),
            sa.Computed("lower(nickname)", persisted=True),
            nullable=False,
        ),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("banned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("banned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_hidden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_nickname_user_id"),
        sa.UniqueConstraint("nickname_ci", name="uq_user_nickname_nickname_ci"),
    )
    # Prefix-search index — enables `WHERE nickname_ci LIKE 'ali%'`.
    op.execute(
        "CREATE INDEX ix_user_nickname_nickname_ci_prefix "
        "ON user_nickname (nickname_ci text_pattern_ops)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_user_nickname_nickname_ci_prefix")
    op.drop_table("user_nickname")


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
        {"name": table_name},
    )
    return result.scalar() is not None
