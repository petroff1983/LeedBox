"""Isolated AI balances and idempotent operations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a913c72e610b"
down_revision = "f804e53b291a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_accounts",
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("bot_id", sa.BigInteger(), primary_key=True),
        sa.Column("test_mode", sa.Boolean(), primary_key=True),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("reserved", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "balance >= 0 AND reserved >= 0 AND reserved <= balance", name="ai_funds"
        ),
    )
    op.create_table(
        "ai_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("action_key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("reserve_tokens", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger()),
        sa.Column("cost_usd", sa.Numeric(20, 8), nullable=False),
        sa.Column("cost_rub", sa.Numeric(20, 6), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("bot_id", "action_key"),
        sa.CheckConstraint(
            "status IN ('reserved', 'sent', 'completed', 'uncertain', 'failed')", name="ai_status"
        ),
        sa.CheckConstraint("kind IN ('generation', 'purchase', 'grant')", name="ai_kind"),
        sa.CheckConstraint(
            "reserve_tokens >= 0 AND input_tokens >= 0 AND output_tokens >= 0", name="ai_usage"
        ),
    )
    op.create_index("ix_ai_operations_owner_user_id", "ai_operations", ["owner_user_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM ai_operations)")):
        raise RuntimeError("AI financial history exists; destructive downgrade refused")
    op.drop_table("ai_operations")
    op.drop_table("ai_accounts")
