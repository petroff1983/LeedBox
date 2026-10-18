"""Payment operations and confirmed full refunds.

Revision ID: a207dc51e684
Revises: f631a8b25d09
"""

import sqlalchemy as sa
from alembic import op

revision = "a207dc51e684"
down_revision = "f631a8b25d09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("payment_status", "payment_orders", type_="check")
    op.create_check_constraint(
        "payment_status",
        "payment_orders",
        "status IN ('pending', 'paid', 'cancelled', 'review', 'refunded')",
    )
    op.add_column("payment_orders", sa.Column("refunded_at", sa.DateTime(timezone=True)))
    op.create_table(
        "payment_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("payment_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger()),
        sa.Column("provider_ref", sa.String(200)),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("outcome", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("order_id", "kind"),
        sa.CheckConstraint("kind IN ('refund', 'reconcile')", name="payment_operation_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'submitted', 'done', 'failed', 'uncertain')",
            name="payment_operation_status",
        ),
    )
    op.create_index("ix_payment_operations_order_id", "payment_operations", ["order_id"])


def downgrade() -> None:
    # Refuse data loss; never hide a confirmed refund when downgrading.
    if (
        op.get_bind().execute(sa.text("SELECT count(*) FROM payment_operations")).scalar()
        or op.get_bind()
        .execute(sa.text("SELECT count(*) FROM payment_orders WHERE status='refunded'"))
        .scalar()
    ):
        raise RuntimeError("Cannot downgrade nonempty payment operations or refunded orders")
    op.drop_table("payment_operations")
    op.drop_column("payment_orders", "refunded_at")
    op.drop_constraint("payment_status", "payment_orders", type_="check")
    op.create_check_constraint(
        "payment_status", "payment_orders", "status IN ('pending', 'paid', 'cancelled', 'review')"
    )
