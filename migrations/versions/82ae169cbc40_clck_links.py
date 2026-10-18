"""Persist provider results without changing previously issued links."""

import sqlalchemy as sa
from alembic import op

revision = "82ae169cbc40"
down_revision = "7c8a42d901ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracked_links", sa.Column("external_url", sa.Text(), nullable=True))
    op.add_column("tracked_links", sa.Column("external_id", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_links", "external_id")
    op.drop_column("tracked_links", "external_url")
