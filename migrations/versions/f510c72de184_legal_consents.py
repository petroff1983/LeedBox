"""Immutable legal publications use content_versions; consent events are separate."""

import sqlalchemy as sa
from alembic import op

revision = "f510c72de184"
down_revision = "f492e317ca80"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "legal_consent_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column(
            "version_id",
            sa.BigInteger(),
            sa.ForeignKey("content_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "policy_version_id",
            sa.BigInteger(),
            sa.ForeignKey("content_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column("action_key", sa.String(100), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("bot_id", "telegram_user_id", "action_key"),
        sa.CheckConstraint("action IN ('grant', 'revoke')", name="legal_consent_action"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM legal_consent_events)")):
        raise RuntimeError("Consent evidence must not be erased by a schema downgrade")
    op.drop_table("legal_consent_events")
