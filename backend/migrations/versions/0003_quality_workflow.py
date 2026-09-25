"""Persist DP-01 bases, decisions, context and ERP outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003_quality_workflow"
down_revision = "0002_item_traceability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_items",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("context_revision", sa.Integer, nullable=False),
        sa.Column("state", pg.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("context_revision >= 0", name="ck_quality_item_revision"),
    )
    op.create_table(
        "quality_bases",
        sa.Column("basis_id", sa.String(128), primary_key=True),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("context_revision", sa.Integer, nullable=False),
        sa.Column("target_id", sa.String(128)),
        sa.Column("snapshot", pg.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quality_bases_item_id", "quality_bases", ["item_id"])
    op.create_table(
        "quality_commands",
        sa.Column("actor_id", sa.String(128), primary_key=True),
        sa.Column("command_type", sa.String(64), primary_key=True),
        sa.Column("target_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("result", pg.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "quality_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("command_type", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("decision", pg.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quality_decisions_item_id", "quality_decisions", ["item_id"])
    op.create_table(
        "quality_outbox",
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("result_version", sa.Integer, nullable=False),
        sa.Column("previous_message_id", sa.String(128)),
        sa.Column("context_revision", sa.Integer, nullable=False),
        sa.Column("payload", pg.JSONB, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("ack", pg.JSONB),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("item_id", "result_version", name="uq_quality_outbox_item_version"),
    )
    op.create_index("ix_quality_outbox_item_id", "quality_outbox", ["item_id"])
    op.execute("""
        CREATE FUNCTION reject_quality_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'quality history is append-only' USING ERRCODE = '42501';
        END;
        $$
    """)
    for table, trigger in (
        ("quality_bases", "quality_bases_no_update_delete"),
        ("quality_commands", "quality_commands_no_update_delete"),
        ("quality_decisions", "quality_decisions_no_update_delete"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_quality_history_mutation()
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive quality-history downgrade is disabled; restore a verified backup."
    )
