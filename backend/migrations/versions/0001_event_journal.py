"""Encrypted append-only raw events and a separate mutable projection.

Revision ID: 0001_event_journal
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001_event_journal"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_events",
        sa.Column("record_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(128)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("identification_status", sa.String(32), nullable=False),
        sa.Column("time_quality", sa.String(32), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("initial_projection_status", sa.String(32), nullable=False),
        sa.Column("initial_issues", pg.JSONB, nullable=False),
        sa.Column("format_version", sa.Integer, nullable=False),
        sa.Column("crypto_profile_id", sa.String(64), nullable=False),
        sa.Column("key_id", sa.String(128), nullable=False),
        sa.Column("key_version", sa.Integer, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.UniqueConstraint("source_id", "event_id", name="uq_raw_events_source_event"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_raw_events_nonce_size"),
        sa.CheckConstraint("octet_length(ciphertext) >= 16", name="ck_raw_events_ciphertext_size"),
    )
    op.create_table(
        "event_projections",
        sa.Column(
            "record_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("raw_events.record_id"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("issues", pg.JSONB, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('applied', 'unresolved')", name="ck_event_projection_status"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_event_projection_revision"),
    )
    op.execute("""
        CREATE FUNCTION reject_raw_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'raw_events is append-only' USING ERRCODE = '42501';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER raw_events_no_update_delete
        BEFORE UPDATE OR DELETE ON raw_events
        FOR EACH ROW EXECUTE FUNCTION reject_raw_event_mutation()
    """)
    op.execute("""
        CREATE TRIGGER raw_events_no_truncate
        BEFORE TRUNCATE ON raw_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_raw_event_mutation()
    """)


def downgrade() -> None:
    # Evidence history must never be silently erased by a migration rollback.
    raise RuntimeError(
        "Destructive journal downgrade is disabled; restore a verified backup instead."
    )
