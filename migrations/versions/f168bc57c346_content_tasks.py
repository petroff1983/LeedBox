"""Durable content-agent input and automatic pipeline checkpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f168bc57c346"
down_revision = "e057af46b235"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "content_tasks",
        sa.Column("token", sa.String(24), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("provider", postgresql.JSONB(), nullable=False),
        sa.Column("operation_id", sa.BigInteger(), sa.ForeignKey("ai_operations.id")),
        sa.Column("search_operation_id", sa.BigInteger(), sa.ForeignKey("ai_operations.id")),
        sa.Column("screen_id", sa.BigInteger()),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column(
            "ready_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('draft','queued','working','waiting',"
            "'completed','failed','uncertain','cancelled')",
            name="content_task_status",
        ),
    )
    for field in ("owner_user_id", "status", "ready_at"):
        op.create_index("ix_content_tasks_" + field, "content_tasks", [field])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM content_tasks)")):
        raise RuntimeError("Content task history must not be erased; restore a backup instead")
    op.drop_table("content_tasks")
