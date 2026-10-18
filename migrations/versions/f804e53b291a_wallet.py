"""Bot-scoped ruble ledger and Robokassa topups."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f804e53b291a"
down_revision = "e731ac285b62"
branch_labels = None
depends_on = None


def routes(topup):
    op.drop_constraint("payment_kind", "payment_orders", type_="check")
    op.drop_constraint("payment_route", "payment_orders", type_="check")
    kinds = "'service', 'topup'" if topup else "'service'"
    op.create_check_constraint(
        "payment_kind", "payment_orders", "kind IN ('access', " + kinds + ")"
    )
    op.create_check_constraint(
        "payment_route",
        "payment_orders",
        "(kind = 'access' AND provider = 'stars' AND currency = 'XTR' AND period_days > 0) "
        "OR (kind IN ("
        + kinds
        + ") AND provider = 'robokassa' AND currency = 'RUB' AND period_days = 0)",
    )


def upgrade():
    routes(True)
    op.create_table(
        "wallet_accounts",
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("bot_id", sa.BigInteger(), primary_key=True),
        sa.Column("test_mode", sa.Boolean(), primary_key=True),
        sa.Column("balance", sa.Numeric(20, 2), nullable=False),
    )
    op.create_table(
        "wallet_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(20, 2), nullable=False),
        sa.Column("action_key", sa.String(100), nullable=False),
        sa.Column(
            "order_id", sa.BigInteger(), sa.ForeignKey("payment_orders.id", ondelete="RESTRICT")
        ),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("bot_id", "action_key"),
        sa.CheckConstraint("kind IN ('topup', 'purchase', 'refund')", name="wallet_entry_kind"),
        sa.CheckConstraint("amount <> 0", name="wallet_entry_amount"),
    )
    op.create_index("ix_wallet_entries_owner_user_id", "wallet_entries", ["owner_user_id"])


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM wallet_entries) "
            "OR EXISTS (SELECT 1 FROM payment_orders WHERE kind='topup')"
        )
    ):
        raise RuntimeError("Wallet financial records exist; destructive downgrade refused")
    op.drop_table("wallet_entries")
    op.drop_table("wallet_accounts")
    routes(False)
