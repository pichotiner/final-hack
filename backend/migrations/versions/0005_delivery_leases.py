"""Separate durable delivery scheduling from immutable result snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0005_delivery_leases"
down_revision = "0004_production_context"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quality_deliveries",
        sa.Column(
            "message_id",
            sa.String(128),
            sa.ForeignKey("quality_outbox.message_id"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(128)),
        sa.Column("last_error", sa.String(128)),
        sa.CheckConstraint("attempts >= 0", name="ck_quality_delivery_attempts"),
    )
    op.execute("""INSERT INTO quality_deliveries
        (message_id, status, attempts, next_attempt_at)
        SELECT message_id, status, attempts, created_at FROM quality_outbox""")


def downgrade():
    raise RuntimeError("Destructive history downgrade is disabled; restore a verified backup.")
