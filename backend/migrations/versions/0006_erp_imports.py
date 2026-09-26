"""Immutable external work-order snapshots and atomic import receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_erp_imports"
down_revision = "0005_delivery_leases"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "quality_erp_imports",
        sa.Column("external_system", sa.String(128), primary_key=True),
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column("external_order_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("profile_version", sa.String(128), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("document", postgresql.JSONB, nullable=False),
        sa.Column("profile", postgresql.JSONB, nullable=False),
        sa.Column("receipt", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "external_system", "external_order_id", "revision", name="uq_erp_order_revision"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_erp_order_revision"),
    )
    op.execute("""CREATE TRIGGER quality_erp_imports_immutable
        BEFORE UPDATE OR DELETE ON quality_erp_imports
        FOR EACH ROW EXECUTE FUNCTION reject_quality_history_mutation()""")
    op.execute("""CREATE TRIGGER quality_erp_imports_no_truncate
        BEFORE TRUNCATE ON quality_erp_imports
        FOR EACH STATEMENT EXECUTE FUNCTION reject_quality_history_mutation()""")


def downgrade():
    raise RuntimeError("Destructive history downgrade is disabled; restore a verified backup.")
