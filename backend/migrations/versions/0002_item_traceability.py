"""Versioned, replayable item histories separate from encrypted evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002_item_traceability"
down_revision = "0001_event_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_raw_events_item_id", "raw_events", ["item_id"])
    op.create_table(
        "item_projections",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", pg.JSONB, nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_item_projection_revision"),
    )
    op.create_table(
        "item_projection_versions",
        sa.Column(
            "item_id", sa.String(128), sa.ForeignKey("item_projections.item_id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("processor_version", sa.String(64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot", pg.JSONB, nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_item_projection_version_revision"),
    )
    op.execute("""
        CREATE FUNCTION reject_history_version_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'item_projection_versions is append-only' USING ERRCODE = '42501';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER item_history_no_update_delete
        BEFORE UPDATE OR DELETE ON item_projection_versions
        FOR EACH ROW EXECUTE FUNCTION reject_history_version_mutation()
    """)
    op.execute("""
        CREATE TRIGGER item_history_no_truncate
        BEFORE TRUNCATE ON item_projection_versions
        FOR EACH STATEMENT EXECUTE FUNCTION reject_history_version_mutation()
    """)


def downgrade() -> None:
    raise RuntimeError("Destructive history downgrade is disabled; restore a verified backup.")
