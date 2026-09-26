from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert
from sqlalchemy.engine import Connection, Engine, RowMapping

from app.adapters.locks import production_guard
from app.domain.errors import InvalidTransition
from app.domain.events import (
    EventEnvelope,
    EventType,
    ProtectedPayload,
    RecordMetadata,
    StoredEvent,
)

metadata = MetaData()

raw_events = Table(
    "raw_events",
    metadata,
    Column("record_id", UUID(as_uuid=True), primary_key=True),
    Column("source_id", String(128), nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("schema_version", String(32), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("item_id", String(128)),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("identification_status", String(32), nullable=False),
    Column("time_quality", String(32), nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("initial_projection_status", String(32), nullable=False),
    Column("initial_issues", JSONB, nullable=False),
    Column("format_version", Integer, nullable=False),
    Column("crypto_profile_id", String(64), nullable=False),
    Column("key_id", String(128), nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    UniqueConstraint("source_id", "event_id", name="uq_raw_events_source_event"),
    CheckConstraint("octet_length(nonce) = 12", name="ck_raw_events_nonce_size"),
    CheckConstraint("octet_length(ciphertext) >= 16", name="ck_raw_events_ciphertext_size"),
)

event_projections = Table(
    "event_projections",
    metadata,
    Column("record_id", UUID(as_uuid=True), ForeignKey("raw_events.record_id"), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("issues", JSONB, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('applied', 'unresolved')", name="ck_event_projection_status"),
    CheckConstraint("revision >= 1", name="ck_event_projection_revision"),
)

item_projections = Table(
    "item_projections",
    metadata,
    Column("item_id", String(128), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("processor_version", String(64), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("snapshot", JSONB, nullable=False),
    CheckConstraint("revision >= 1", name="ck_item_projection_revision"),
)

item_projection_versions = Table(
    "item_projection_versions",
    metadata,
    Column("item_id", String(128), ForeignKey("item_projections.item_id"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("processor_version", String(64), nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    Column("snapshot", JSONB, nullable=False),
    CheckConstraint("revision >= 1", name="ck_item_projection_version_revision"),
)

Index("ix_raw_events_item_id", raw_events.c.item_id)

quality_items = Table(
    "quality_items",
    metadata,
    Column("item_id", String(128), primary_key=True),
    Column("context_revision", Integer, nullable=False),
    Column("storage_revision", Integer, nullable=False),
    Column("state", JSONB, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("context_revision >= 0", name="ck_quality_item_revision"),
)

quality_bases = Table(
    "quality_bases",
    metadata,
    Column("basis_id", String(128), primary_key=True),
    Column("item_id", String(128), nullable=False),
    Column("context_revision", Integer, nullable=False),
    Column("target_id", String(128)),
    Column("snapshot", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

quality_commands = Table(
    "quality_commands",
    metadata,
    Column("actor_id", String(128), primary_key=True),
    Column("command_type", String(64), primary_key=True),
    Column("target_id", String(128), primary_key=True),
    Column("idempotency_key", String(128), primary_key=True),
    Column("command_digest", String(64), nullable=False),
    Column("result", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

quality_decisions = Table(
    "quality_decisions",
    metadata,
    Column("decision_id", String(128), primary_key=True),
    Column("item_id", String(128), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("command_type", String(64), nullable=False),
    Column("action", String(64), nullable=False),
    Column("decision", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

quality_outbox = Table(
    "quality_outbox",
    metadata,
    Column("message_id", String(128), primary_key=True),
    Column("item_id", String(128), nullable=False),
    Column("result_version", Integer, nullable=False),
    Column("previous_message_id", String(128)),
    Column("context_revision", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("ack", JSONB),
    Column("attempts", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("item_id", "result_version", name="uq_quality_outbox_item_version"),
)

Index("ix_quality_bases_item_id", quality_bases.c.item_id)
Index("ix_quality_decisions_item_id", quality_decisions.c.item_id)
Index("ix_quality_outbox_item_id", quality_outbox.c.item_id)

quality_deliveries = Table(
    "quality_deliveries",
    metadata,
    Column("message_id", String(128), ForeignKey("quality_outbox.message_id"), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False),
    Column("lease_until", DateTime(timezone=True)),
    Column("lease_token", String(128)),
    Column("last_error", String(128)),
    CheckConstraint("attempts >= 0", name="ck_quality_delivery_attempts"),
)

quality_constraints = Table(
    "quality_constraints",
    metadata,
    Column("constraint_id", String(128), primary_key=True),
    Column("is_open", Boolean, nullable=False),
    Column("state", JSONB, nullable=False),
)

quality_context_versions = Table(
    "quality_context_versions",
    metadata,
    Column("import_id", String(128), primary_key=True),
    Column("item_id", String(128), nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

quality_external_streams = Table(
    "quality_external_streams",
    metadata,
    Column("item_id", String(128), primary_key=True),
    Column("producer_id", String(128), nullable=False),
    Column("external_system", String(128), nullable=False),
    Column("external_order_id", String(128), nullable=False),
    Column("external_item_id", String(128), nullable=False),
    UniqueConstraint(
        "producer_id",
        "external_system",
        "external_order_id",
        "external_item_id",
        name="uq_quality_external_stream",
    ),
)

quality_state_versions = Table(
    "quality_state_versions",
    metadata,
    Column("item_id", String(128), primary_key=True),
    Column("storage_revision", Integer, primary_key=True),
    Column("context_revision", Integer, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

quality_erp_imports = Table(
    "quality_erp_imports",
    metadata,
    Column("external_system", String(128), primary_key=True),
    Column("message_id", String(128), primary_key=True),
    Column("external_order_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("content_digest", String(64), nullable=False),
    Column("profile_id", String(128), nullable=False),
    Column("profile_version", String(128), nullable=False),
    Column("profile_digest", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
    Column("profile", JSONB, nullable=False),
    Column("receipt", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "external_system", "external_order_id", "revision", name="uq_erp_order_revision"
    ),
    CheckConstraint("revision >= 1", name="ck_erp_order_revision"),
)


def _values(event: StoredEvent) -> dict:
    meta, envelope, protection = event.metadata, event.metadata.envelope, event.protection
    return {
        "record_id": meta.record_id,
        "source_id": envelope.source_id,
        "event_id": envelope.event_id,
        "schema_version": envelope.schema_version,
        "event_type": envelope.event_type.value,
        "item_id": envelope.item_id,
        "occurred_at": envelope.occurred_at,
        "received_at": meta.received_at,
        "identification_status": envelope.identification_status,
        "time_quality": envelope.time_quality,
        "content_digest": meta.content_digest,
        "initial_projection_status": meta.initial_projection_status,
        "initial_issues": list(meta.initial_issues),
        "format_version": protection.format_version,
        "crypto_profile_id": protection.crypto_profile_id,
        "key_id": protection.key_id,
        "key_version": protection.key_version,
        "nonce": protection.nonce,
        "ciphertext": protection.ciphertext,
    }


def _record(row: RowMapping) -> StoredEvent:
    envelope = EventEnvelope(
        source_id=row["source_id"],
        event_id=row["event_id"],
        schema_version=row["schema_version"],
        event_type=EventType(row["event_type"]),
        item_id=row["item_id"],
        occurred_at=row["occurred_at"],
        identification_status=row["identification_status"],
        time_quality=row["time_quality"],
    )
    return StoredEvent(
        RecordMetadata(
            record_id=row["record_id"],
            envelope=envelope,
            received_at=row["received_at"],
            content_digest=row["content_digest"],
            initial_projection_status=row["initial_projection_status"],
            initial_issues=tuple(row["initial_issues"]),
        ),
        ProtectedPayload(
            format_version=row["format_version"],
            crypto_profile_id=row["crypto_profile_id"],
            key_id=row["key_id"],
            key_version=row["key_version"],
            nonce=row["nonce"],
            ciphertext=row["ciphertext"],
        ),
    )


class PostgresEventJournal:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("This journal requires PostgreSQL.")
        self.engine = engine

    def append_if_absent(self, event: StoredEvent) -> tuple[StoredEvent, bool]:
        # PostgreSQL's unique constraint serializes contenders for the same key.
        # A separate SELECT sees the winner under READ COMMITTED after waiting.
        # Never emit an accepted receipt until the transaction has committed.
        with self.engine.begin() as connection:
            production_guard(connection)
            if (
                connection.scalar(
                    select(quality_items.c.item_id).where(
                        quality_items.c.item_id == event.metadata.envelope.item_id
                    )
                )
                is not None
            ):
                raise InvalidTransition("Use the production journal for quality-managed items.")
            return self._append(connection, event)

    def _append(self, connection: Connection, event: StoredEvent) -> tuple[StoredEvent, bool]:
        """Persist within the caller's transaction, never commit independently."""
        envelope = event.metadata.envelope
        inserted_id = connection.execute(
            insert(raw_events)
            .values(**_values(event))
            .on_conflict_do_nothing(constraint="uq_raw_events_source_event")
            .returning(raw_events.c.record_id)
        ).scalar_one_or_none()
        inserted = inserted_id is not None
        if inserted:
            connection.execute(
                event_projections.insert().values(
                    record_id=event.metadata.record_id,
                    status=event.metadata.initial_projection_status,
                    issues=list(event.metadata.initial_issues),
                    revision=1,
                    updated_at=event.metadata.received_at,
                )
            )
        row = (
            connection.execute(
                select(raw_events).where(
                    raw_events.c.source_id == envelope.source_id,
                    raw_events.c.event_id == envelope.event_id,
                )
            )
            .mappings()
            .one()
        )
        return _record(row), inserted

    def get(self, source_id: str, event_id: str) -> StoredEvent | None:
        """Return a protected record. Callers must authenticate it before use."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(raw_events).where(
                        raw_events.c.source_id == source_id,
                        raw_events.c.event_id == event_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _record(row)
