"""Keep paid provider searches separate from text generations in the ledger."""

import sqlalchemy as sa
from alembic import op

revision = "e057af46b235"
down_revision = "d946fe35a124"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ai_kind", "ai_operations", type_="check")
    op.create_check_constraint(
        "ai_kind", "ai_operations", "kind IN ('generation', 'purchase', 'grant', 'search')"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM ai_operations WHERE kind = 'search')")
    ):
        raise RuntimeError("Search billing history must not be erased; restore a backup instead")
    op.drop_constraint("ai_kind", "ai_operations", type_="check")
    op.create_check_constraint(
        "ai_kind", "ai_operations", "kind IN ('generation', 'purchase', 'grant')"
    )
