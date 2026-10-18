"""Versioned catalogs and explicit niche suggestions.

Revision ID: 9e102ad7b541
Revises: 4b32afde91c0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9e102ad7b541"
down_revision = "4b32afde91c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("niches", "lead_magnet_ideas", "templates"):
        op.create_table(
            table,
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("title", sa.String(100), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint("slug", name="uq_" + table + "_slug"),
        )
    op.create_table(
        "niche_suggestions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_niche_suggestions_owner_user_id"),
    )
    op.create_index("ix_niche_suggestions_owner_user_id", "niche_suggestions", ["owner_user_id"])


def downgrade() -> None:
    op.drop_table("niche_suggestions")
    for table in ("templates", "lead_magnet_ideas", "niches"):
        op.drop_table(table)
