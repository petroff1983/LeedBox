"""Immutable expert-scoped Dzen search snapshots, independent of FSM."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b724dc138f02"
down_revision = "a913c72e610b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dzen_searches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("owner_user_id", "bot_id", "test_mode", "token"),
    )
    op.create_index("ix_dzen_searches_owner_user_id", "dzen_searches", ["owner_user_id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM dzen_searches)")):
        raise RuntimeError("Saved search history exists; destructive downgrade refused")
    op.drop_table("dzen_searches")
