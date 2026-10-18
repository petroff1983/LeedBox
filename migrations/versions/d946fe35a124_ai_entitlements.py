"""Per-account AI entitlements and immutable operation funding source."""

import sqlalchemy as sa
from alembic import op

revision = "d946fe35a124"
down_revision = "c835ed249013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_accounts",
        sa.Column("unlimited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "ai_accounts",
        sa.Column(
            "free_limits", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "ai_operations",
        sa.Column("billing_mode", sa.String(12), nullable=False, server_default="balance"),
    )
    op.create_check_constraint(
        "ai_billing_mode", "ai_operations", "billing_mode IN ('balance', 'trial', 'unlimited')"
    )


def downgrade():
    changed = op.get_bind().scalar(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM ai_accounts WHERE unlimited OR free_limits <> '{}'::jsonb)
            OR EXISTS (SELECT 1 FROM ai_operations WHERE billing_mode <> 'balance')
    """)
    )
    if changed:
        raise RuntimeError(
            "AI entitlement downgrade would erase billing history; restore a backup instead"
        )
    op.drop_constraint("ai_billing_mode", "ai_operations", type_="check")
    op.drop_column("ai_operations", "billing_mode")
    op.drop_column("ai_accounts", "free_limits")
    op.drop_column("ai_accounts", "unlimited")
