"""payment order snapshots and deduplicated confirmations

Revision ID: e91246c0ab35
Revises: d83972ac1f04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e91246c0ab35"
down_revision = "d83972ac1f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(12), nullable=False),
        sa.Column("account", sa.String(100), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("action_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("bot_id", "action_key"),
        sa.CheckConstraint("kind IN ('access', 'service')", name="payment_kind"),
        sa.CheckConstraint("provider IN ('stars', 'robokassa')", name="payment_provider"),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'cancelled', 'review')", name="payment_status"
        ),
        sa.CheckConstraint("amount > 0", name="payment_amount"),
        sa.CheckConstraint(
            "(kind = 'access' AND provider = 'stars' AND currency = 'XTR' AND period_days > 0) "
            "OR (kind = 'service' AND provider = 'robokassa' AND currency = 'RUB' "
            "AND period_days = 0)",
            name="payment_route",
        ),
    )
    op.create_index("ix_payment_orders_owner_user_id", "payment_orders", ["owner_user_id"])
    op.create_table(
        "payment_receipts",
        sa.Column("order_id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.String(12), nullable=False),
        sa.Column("account", sa.String(100), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("charge_id", sa.String(200), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["payment_orders.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("provider", "account", "test_mode", "charge_id"),
    )


def downgrade() -> None:
    op.drop_table("payment_receipts")
    op.drop_index("ix_payment_orders_owner_user_id", "payment_orders")
    op.drop_table("payment_orders")
