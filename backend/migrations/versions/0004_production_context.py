"""Persist canonical context imports and conservative pilot-wide constraints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004_production_context"
down_revision = "0003_quality_workflow"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "quality_items",
        sa.Column("storage_revision", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "quality_constraints",
        sa.Column("constraint_id", sa.String(128), primary_key=True),
        sa.Column("is_open", sa.Boolean, nullable=False),
        sa.Column("state", pg.JSONB, nullable=False),
    )
    op.create_table(
        "quality_context_versions",
        sa.Column("import_id", sa.String(128), primary_key=True),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("snapshot", pg.JSONB, nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "quality_external_streams",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("producer_id", sa.String(128), nullable=False),
        sa.Column("external_system", sa.String(128), nullable=False),
        sa.Column("external_order_id", sa.String(128), nullable=False),
        sa.Column("external_item_id", sa.String(128), nullable=False),
        sa.UniqueConstraint(
            "producer_id",
            "external_system",
            "external_order_id",
            "external_item_id",
            name="uq_quality_external_stream",
        ),
    )
    op.create_table(
        "quality_state_versions",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("storage_revision", sa.Integer, primary_key=True),
        sa.Column("context_revision", sa.Integer, nullable=False),
        sa.Column("snapshot", pg.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""INSERT INTO quality_state_versions
        (item_id, storage_revision, context_revision, snapshot, created_at)
        SELECT item_id, 0, context_revision, state, updated_at FROM quality_items""")
    op.execute("""CREATE TRIGGER quality_context_versions_immutable
        BEFORE UPDATE OR DELETE ON quality_context_versions
        FOR EACH ROW EXECUTE FUNCTION reject_quality_history_mutation()""")
    for table in ("quality_state_versions", "quality_external_streams"):
        op.execute(f"""CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_quality_history_mutation()""")
    for table in (
        "quality_context_versions",
        "quality_bases",
        "quality_decisions",
        "quality_commands",
        "quality_state_versions",
        "quality_external_streams",
    ):
        op.execute(f"""CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_quality_history_mutation()""")
    op.execute("""CREATE FUNCTION protect_quality_outbox_body() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'outbox history cannot be deleted' USING ERRCODE = '42501';
            END IF;
            IF (to_jsonb(NEW) - ARRAY['status','ack','attempts']) IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status','ack','attempts']) THEN
                RAISE EXCEPTION 'outbox body is immutable' USING ERRCODE = '42501';
            END IF;
            RETURN NEW;
        END; $$""")
    op.execute("""CREATE TRIGGER quality_outbox_body_immutable BEFORE UPDATE OR DELETE
        ON quality_outbox FOR EACH ROW EXECUTE FUNCTION protect_quality_outbox_body()""")
    op.execute("""CREATE TRIGGER quality_outbox_no_truncate BEFORE TRUNCATE
        ON quality_outbox FOR EACH STATEMENT EXECUTE FUNCTION reject_quality_history_mutation()""")


def downgrade():
    raise RuntimeError("Destructive history downgrade is disabled; restore a verified backup.")
