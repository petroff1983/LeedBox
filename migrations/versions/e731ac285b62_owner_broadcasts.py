"""Owner-only broadcasts and bot-scoped expert notification preferences."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e731ac285b62"
down_revision = "d94021c384ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expert_notification_preferences",
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("bot_id", sa.BigInteger(), primary_key=True),
        sa.Column("notices", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("advertising", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("advertising_granted_at", sa.DateTime(timezone=True)),
        sa.Column("advertising_revoked_at", sa.DateTime(timezone=True)),
        sa.Column("consent_version", sa.String(40)),
    )
    # Existing private expert sessions establish bot membership, never advertising consent.
    op.execute("""
        INSERT INTO expert_notification_preferences (owner_user_id, bot_id)
        SELECT DISTINCT u.id, split_part(w.key, ':', 1)::bigint
        FROM users u JOIN wizard_drafts w ON split_part(w.key, ':', 3) = u.telegram_id::text
        WHERE u.expert_started_at IS NOT NULL
          AND split_part(w.key, ':', 1) ~ '^[1-9][0-9]{0,17}$'
          AND split_part(w.key, ':', 2) = u.telegram_id::text
        ON CONFLICT DO NOTHING
    """)
    op.create_table(
        "owner_broadcasts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(15), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('notice', 'advertising')", name="broadcast_kind"),
        sa.CheckConstraint("status IN ('draft', 'queued', 'cancelled')", name="broadcast_status"),
    )
    op.create_index("ix_owner_broadcasts_bot_id", "owner_broadcasts", ["bot_id"])
    op.add_column("outbox_messages", sa.Column("broadcast_id", sa.BigInteger()))
    op.create_foreign_key(
        "fk_outbox_broadcast",
        "outbox_messages",
        "owner_broadcasts",
        ["broadcast_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_outbox_messages_broadcast_id", "outbox_messages", ["broadcast_id"])


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_broadcast_id", table_name="outbox_messages")
    op.drop_constraint("fk_outbox_broadcast", "outbox_messages", type_="foreignkey")
    op.drop_column("outbox_messages", "broadcast_id")
    op.drop_table("owner_broadcasts")
    op.drop_table("expert_notification_preferences")
