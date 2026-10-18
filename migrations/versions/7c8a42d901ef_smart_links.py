"""Add owner-scoped short and application links without changing legacy links."""

from alembic import op

revision = "7c8a42d901ef"
down_revision = "b519a04df823"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("tracked_link_kind", "tracked_links", type_="check")
    op.create_check_constraint(
        "tracked_link_kind",
        "tracked_links",
        "kind IN ('expert', 'material', 'invite', 'short', 'deeplink')",
    )


def downgrade() -> None:
    # No data loss on downgrade: PostgreSQL refuses if newer link kinds still exist.
    op.drop_constraint("tracked_link_kind", "tracked_links", type_="check")
    op.create_check_constraint(
        "tracked_link_kind",
        "tracked_links",
        "kind IN ('expert', 'material', 'invite')",
    )
