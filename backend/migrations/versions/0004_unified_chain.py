"""Single-pilot coordinator state and complete transport outbox.

Derived state is mutable; raw events and basis/decision histories remain separate.
This is not an independent completeness anchor or a disaster recovery mechanism.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004_unified_chain"
down_revision = "0003_quality_workflow"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "unified_state",
        sa.Column("namespace", sa.String(128), primary_key=True),
        sa.Column("state", pg.JSONB, nullable=False),
    )


def downgrade():
    raise RuntimeError("Do not destroy the coordinator state; use a verified migration/backup.")
