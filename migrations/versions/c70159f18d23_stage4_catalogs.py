"""Stage 4 catalogs and owner-scoped objection answers."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c70159f18d23"
down_revision = "9e102ad7b541"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("objections", "funnel_topics", "article_categories", "articles"):
        op.create_table(
            name,
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("title", sa.String(100), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint("slug", name=f"uq_{name}_slug"),
        )
    op.create_table(
        "personal_objections",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("objection_id", sa.BigInteger(), nullable=False),
        sa.Column("answer", sa.String(1000), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["objection_id"], ["objections.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "owner_user_id", "objection_id", name="uq_personal_objections_owner_user_id"
        ),
    )
    op.create_index(
        "ix_personal_objections_owner_user_id", "personal_objections", ["owner_user_id"]
    )


def downgrade() -> None:
    op.drop_table("personal_objections")
    for name in ("articles", "article_categories", "funnel_topics", "objections"):
        op.drop_table(name)
