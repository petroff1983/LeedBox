"""Isolated one-time expert login and revocable cabinet sessions.

Revision ID: c628bd912f34
Revises: 82ae169cbc40
"""

import sqlalchemy as sa
from alembic import op

revision = "c628bd912f34"
down_revision = "82ae169cbc40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cabinet_credentials",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("kind IN ('login', 'session')", name="cabinet_credential_kind"),
    )
    op.create_index(
        "ix_cabinet_credentials_owner_user_id", "cabinet_credentials", ["owner_user_id"]
    )
    op.create_index("ix_cabinet_credentials_expires_at", "cabinet_credentials", ["expires_at"])


def downgrade() -> None:
    op.drop_table("cabinet_credentials")
