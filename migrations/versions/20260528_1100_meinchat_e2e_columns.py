"""meinchat S28.3a — e2e columns + conversation protocol/capabilities.

Adds `message.envelope` / `message.protocol` /
`message.delivered_to_all_addressed_devices_at`, drops `body NOT NULL`, swaps
the body-length check for a protocol-gated pair, and adds
`conversation.protocol` / `conversation.capabilities` with a protocol-immutable
trigger (downgrade defence-in-depth; the route also refuses to change it).

Backward-compatible: every existing row keeps protocol='plain', envelope NULL,
body populated.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260528_1100_meinchat_e2e"
down_revision = "20260424_1015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message", sa.Column("envelope", sa.LargeBinary(), nullable=True))
    op.add_column(
        "message",
        sa.Column(
            "protocol",
            sa.String(length=32),
            nullable=False,
            server_default="plain",
        ),
    )
    op.add_column(
        "message",
        sa.Column(
            "delivered_to_all_addressed_devices_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # e2e rows carry body IS NULL (ciphertext lives in envelope).
    op.alter_column("message", "body", existing_type=sa.Text(), nullable=True)

    op.drop_constraint("ck_message_body_len", "message", type_="check")
    op.create_check_constraint(
        "ck_message_body_len",
        "message",
        "protocol <> 'plain' OR (body IS NOT NULL AND length(body) <= 4000)",
    )
    op.create_check_constraint(
        "ck_message_body_or_envelope",
        "message",
        "(protocol = 'plain' AND body IS NOT NULL AND envelope IS NULL)"
        " OR (protocol <> 'plain' AND body IS NULL AND envelope IS NOT NULL)",
    )

    op.add_column(
        "conversation",
        sa.Column(
            "protocol",
            sa.String(length=32),
            nullable=False,
            server_default="plain",
        ),
    )
    op.add_column(
        "conversation",
        sa.Column(
            "capabilities",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Protocol immutability — once pinned, a conversation can never be
    # downgraded. Defence-in-depth alongside the route-level refusal.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION meinchat_raise_protocol_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'conversation protocol is immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_conversation_protocol_immutable
        BEFORE UPDATE OF protocol ON conversation
        FOR EACH ROW WHEN (OLD.protocol IS DISTINCT FROM NEW.protocol)
        EXECUTE FUNCTION meinchat_raise_protocol_immutable();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_conversation_protocol_immutable ON conversation;"
    )
    op.execute("DROP FUNCTION IF EXISTS meinchat_raise_protocol_immutable();")
    op.drop_column("conversation", "capabilities")
    op.drop_column("conversation", "protocol")
    op.drop_constraint("ck_message_body_or_envelope", "message", type_="check")
    op.drop_constraint("ck_message_body_len", "message", type_="check")
    op.create_check_constraint("ck_message_body_len", "message", "length(body) <= 4000")
    op.alter_column("message", "body", existing_type=sa.Text(), nullable=False)
    op.drop_column("message", "delivered_to_all_addressed_devices_at")
    op.drop_column("message", "protocol")
    op.drop_column("message", "envelope")
