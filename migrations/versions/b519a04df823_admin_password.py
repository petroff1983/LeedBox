"""Permanent password authentication for the owner panel.

Revision ID: b519a04df823
Revises: a207dc51e684
"""

import sqlalchemy as sa
from alembic import op

revision = "b519a04df823"
down_revision = "a207dc51e684"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("must_change", sa.Boolean(), nullable=False),
        sa.CheckConstraint("id = 1", name="single_admin_account"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT count(*) FROM admin_accounts")).scalar():
        raise RuntimeError("Cannot downgrade initialized administrator credentials")
    op.drop_table("admin_accounts")
