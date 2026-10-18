"""Separate customer credits from provider tokens; preserve the legacy ledger."""

import sqlalchemy as sa
from alembic import op

revision = "f492e317ca80"
down_revision = "f168bc57c346"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("credit_balance", "credit_reserved"):
        op.add_column(
            "ai_accounts", sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
        )
    op.add_column(
        "ai_accounts",
        sa.Column("credit_value_rub", sa.Numeric(20, 6), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ai_credit_funds",
        "ai_accounts",
        "credit_balance >= 0 AND credit_reserved >= 0 "
        "AND credit_reserved <= credit_balance AND credit_value_rub >= 0",
    )
    op.add_column(
        "ai_operations",
        sa.Column("charged_credits", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ai_operations",
        sa.Column("revenue_rub", sa.Numeric(20, 6), nullable=False, server_default="0"),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM ai_accounts WHERE credit_balance > 0) "
            "OR EXISTS (SELECT 1 FROM ai_operations WHERE charged_credits > 0)"
        )
    ):
        raise RuntimeError("Credit funds/history must not be erased; restore a backup instead")
    op.drop_column("ai_operations", "revenue_rub")
    op.drop_column("ai_operations", "charged_credits")
    op.drop_constraint("ai_credit_funds", "ai_accounts", type_="check")
    for name in ("credit_value_rub", "credit_reserved", "credit_balance"):
        op.drop_column("ai_accounts", name)
