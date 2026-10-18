"""Durable financial updates before polling acknowledgement.

Revision ID: f631a8b25d09
Revises: e91246c0ab35
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f631a8b25d09"
down_revision = "e91246c0ab35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_inbox",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("bot_id", "test_mode", "update_id"),
        sa.CheckConstraint("kind IN ('successful', 'refunded')", name="payment_inbox_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'processed', 'review')", name="payment_inbox_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("payment_inbox")
