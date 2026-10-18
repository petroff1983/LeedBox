"""Project selection and optimistic profile revisions.

Revision ID: 4b32afde91c0
Revises: 90a7cd43e108
"""

import sqlalchemy as sa
from alembic import op

revision = "4b32afde91c0"
down_revision = "90a7cd43e108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("projects", sa.Column("creation_key", sa.String(100), nullable=True))
    op.create_unique_constraint("uq_projects_creation_key", "projects", ["creation_key"])
    op.create_table(
        "project_selections",
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
            name="fk_selection_project_owner",
        ),
    )


def downgrade() -> None:
    op.drop_table("project_selections")
    op.drop_constraint("uq_projects_creation_key", "projects", type_="unique")
    op.drop_column("projects", "creation_key")
    op.drop_column("projects", "revision")
