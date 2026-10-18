"""Named private writing prompt library; immutable AI results remain in the ledger."""

import sqlalchemy as sa
from alembic import op

revision = "c835ed249013"
down_revision = "b724dc138f02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "writing_prompts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column(
            "operation_id",
            sa.BigInteger(),
            sa.ForeignKey("ai_operations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_writing_prompts_owner_user_id", "writing_prompts", ["owner_user_id"])
    op.execute(
        sa.text("""
        INSERT INTO writing_prompts
          (owner_user_id, bot_id, test_mode, operation_id, name, created_at)
        SELECT owner_user_id, bot_id, test_mode, id,
               LEFT(COALESCE(NULLIF(snapshot #>> '{inputs,brief,topic}', ''),
                             'Промпт для статей'), 80),
               created_at
        FROM ai_operations
        WHERE status = 'completed' AND snapshot->>'purpose' = 'writing_profile'
          AND jsonb_typeof(result->'prompt') = 'string'
        ORDER BY id
    """)
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM writing_prompts)")):
        raise RuntimeError("Writing library contains data; destructive downgrade refused")
    op.drop_table("writing_prompts")
