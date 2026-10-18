"""Public YouTube metadata cache (no user content or API keys).

Revision ID: d94021c384ab
Revises: c628bd912f34
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d94021c384ab"
down_revision = "c628bd912f34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "youtube_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_youtube_cache_expires_at", "youtube_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_table("youtube_cache")
