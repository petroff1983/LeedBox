"""Service catalog, scoped requests, consent snapshots and linked outbox.

Revision ID: 60e2c7a93b11
Revises: fa73d25f6ddf
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "60e2c7a93b11"
down_revision = "fa73d25f6ddf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("audience", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("duration", sa.String(150), nullable=False),
        sa.Column("price_label", sa.String(150), nullable=False),
        sa.Column("executor_details", sa.String(500), nullable=False),
        sa.Column("conditions", sa.String(500), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0", name="service_revision_positive"),
    )
    op.create_table(
        "service_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("contact", sa.String(200), nullable=False),
        sa.Column("comment", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action_key", sa.String(100), nullable=False),
        sa.Column("consent_purpose", sa.String(40), nullable=False),
        sa.Column("consent_version", sa.String(64), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
            name="fk_service_request_project_owner",
        ),
        sa.CheckConstraint("status IN ('new', 'working', 'closed')", name="service_request_status"),
        sa.CheckConstraint("revision > 0", name="service_request_revision_positive"),
        sa.UniqueConstraint("action_key"),
    )
    op.create_index("ix_service_requests_owner_user_id", "service_requests", ["owner_user_id"])
    op.create_index("ix_service_requests_bot_id", "service_requests", ["bot_id"])
    op.add_column(
        "outbox_messages", sa.Column("service_request_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_outbox_service_request",
        "outbox_messages",
        "service_requests",
        ["service_request_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_outbox_messages_service_request_id", "outbox_messages", ["service_request_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_messages_service_request_id", table_name="outbox_messages")
    op.drop_constraint("fk_outbox_service_request", "outbox_messages", type_="foreignkey")
    op.drop_column("outbox_messages", "service_request_id")
    op.drop_index("ix_service_requests_bot_id", table_name="service_requests")
    op.drop_index("ix_service_requests_owner_user_id", table_name="service_requests")
    op.drop_table("service_requests")
    op.drop_table("services")
