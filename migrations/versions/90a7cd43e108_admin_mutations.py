"""Idempotent owner catalog creation receipts.

Revision ID: 90a7cd43e108
Revises: 8440bfea217a
"""

import sqlalchemy as sa
from alembic import op

revision = "90a7cd43e108"
down_revision = "8440bfea217a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_mutations",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("entity", sa.String(30), nullable=False),
        sa.Column("object_id", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("admin_mutations")
