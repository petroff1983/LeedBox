"""recommendations and service redirects

Revision ID: d83972ac1f04
Revises: c70159f18d23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d83972ac1f04"
down_revision = "c70159f18d23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recommendations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "recommendation_preferences",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("opted_out", sa.Boolean(), nullable=False),
        sa.Column("session_key", sa.String(32), nullable=False),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", "bot_id"),
    )
    op.create_table(
        "service_redirects",
        sa.Column("code", sa.String(13), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("bot_id", "kind", "entity_id"),
    )
    op.create_table(
        "recommendation_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("partner_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(15), nullable=False),
        sa.Column("event_key", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["partner_id"], ["partners.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("event_key"),
    )


def downgrade() -> None:
    for table in (
        "recommendation_events",
        "service_redirects",
        "recommendation_preferences",
        "recommendations",
    ):
        op.drop_table(table)
