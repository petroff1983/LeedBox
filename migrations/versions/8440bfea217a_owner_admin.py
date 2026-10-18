"""Owner login sessions, immutable content revisions and partner catalog.

Revision ID: 8440bfea217a
Revises: 60e2c7a93b11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "8440bfea217a"
down_revision = "60e2c7a93b11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_login_attempts",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("signature_hash", sa.String(64), unique=True),
    )
    op.create_index("ix_admin_login_attempts_expires_at", "admin_login_attempts", ["expires_at"])
    op.create_table(
        "admin_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])
    op.create_table(
        "content_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("entity", sa.String(30), nullable=False),
        sa.Column("object_key", sa.String(100), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("entity", "object_key", "number"),
    )
    op.create_table(
        "partners",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("advertiser", sa.String(300), nullable=False),
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("erid", sa.String(200), nullable=False),
        sa.Column("marking_text", sa.String(500), nullable=False),
        sa.Column("exception_reason", sa.String(1000), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("partners")
    op.drop_table("content_versions")
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_table("admin_sessions")
    op.drop_index("ix_admin_login_attempts_expires_at", table_name="admin_login_attempts")
    op.drop_table("admin_login_attempts")
