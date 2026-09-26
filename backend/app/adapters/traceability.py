"""Transactional per-item replay, using PostgreSQL rather than process-local locks."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from app.adapters.json_events import JsonEventCodec
from app.adapters.locks import lock as _lock
from app.adapters.locks import production_guard
from app.adapters.postgres import (
    PostgresEventJournal,
    _record,
    event_projections,
    item_projection_versions,
    item_projections,
    quality_items,
    raw_events,
)
from app.domain.errors import IntegrityViolation, InvalidTransition
from app.domain.events import StoredEvent
from app.domain.traceability import (
    PROCESSOR_VERSION,
    ItemHistory,
    TraceFact,
    rebuild_history,
    trace_fact,
    unlinked_state,
)
from app.ports.journal import EventProtector


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported projection value.")


class PostgresTraceabilityJournal(PostgresEventJournal):
    """Internal ingestion adapter; public authentication/DTO validation is separate."""

    def __init__(self, engine: Engine, protector: EventProtector) -> None:
        super().__init__(engine)
        self.protector = protector
        self.codec = JsonEventCodec()
        if engine.dialect.name == "postgresql":
            with engine.connect() as connection:
                if connection.get_isolation_level() != "READ COMMITTED":
                    raise ValueError("Traceability replay requires READ COMMITTED isolation.")

    def _fact(self, event: StoredEvent) -> tuple[bytes, TraceFact]:
        raw = self.protector.open(event.protection, event.metadata)
        prepared = self.codec.decode(raw)
        if prepared.envelope != event.metadata.envelope or not compare_digest(
            prepared.content_digest, event.metadata.content_digest
        ):
            raise IntegrityViolation("Stored metadata does not match the original event.")
        return raw, trace_fact(event.metadata, json.loads(raw))

    def _facts(self, connection: Connection, item_id: str) -> list[TraceFact]:
        rows = connection.execute(
            select(raw_events).where(
                raw_events.c.item_id == item_id,
                raw_events.c.identification_status == "reliable",
            )
        ).mappings()
        return [self._fact(_record(row))[1] for row in rows]

    def append_if_absent(self, event: StoredEvent) -> tuple[StoredEvent, bool]:
        envelope = event.metadata.envelope
        with self.engine.begin() as connection:
            production_guard(connection)
            if (
                connection.scalar(
                    select(quality_items.c.item_id).where(
                        quality_items.c.item_id == envelope.item_id
                    )
                )
                is not None
            ):
                raise InvalidTransition("Use the production journal for quality-managed items.")
            # Always event key before item key. Replay only takes the item key.
            _lock(connection, "event", envelope.source_id, envelope.event_id)
            row = (
                connection.execute(
                    select(raw_events).where(
                        raw_events.c.source_id == envelope.source_id,
                        raw_events.c.event_id == envelope.event_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                # IngestEvent authenticates and compares before issuing a receipt.
                return _record(row), False
            raw, candidate = self._fact(event)
            history = None
            if envelope.item_id is not None and envelope.identification_status == "reliable":
                _lock(connection, "item", envelope.item_id)
                history = rebuild_history(
                    envelope.item_id, [*self._facts(connection, envelope.item_id), candidate]
                )
                entry = next(e for e in history.timeline if e.record_id == event.metadata.record_id)
                status, issues = entry.projection_status, entry.issues
            else:
                state = unlinked_state(candidate)
                status, issues = state.status, state.issues
            meta = replace(event.metadata, initial_projection_status=status, initial_issues=issues)
            protected = StoredEvent(meta, self.protector.seal(raw, meta))
            stored, inserted = self._append(connection, protected)
            # A legacy writer might win the unique key without taking our locks.
            if inserted and history is not None:
                self._save(connection, history)
            return stored, inserted

    def _save(self, connection: Connection, history: ItemHistory) -> dict:
        snapshot = json.loads(json.dumps(asdict(history), default=_json_value))
        current = (
            connection.execute(
                select(item_projections).where(item_projections.c.item_id == history.item_id)
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(UTC)
        if current is not None and current["snapshot"] == snapshot:
            result = self._view(current, "updated_at")
        else:
            revision = current["revision"] + 1 if current else 1
            values = dict(
                item_id=history.item_id,
                revision=revision,
                processor_version=PROCESSOR_VERSION,
                updated_at=now,
                snapshot=snapshot,
            )
            connection.execute(
                insert(item_projections)
                .values(**values)
                .on_conflict_do_update(index_elements=[item_projections.c.item_id], set_=values)
            )
            connection.execute(
                item_projection_versions.insert().values(
                    item_id=history.item_id,
                    revision=revision,
                    processor_version=PROCESSOR_VERSION,
                    computed_at=now,
                    snapshot=snapshot,
                )
            )
            result = {**snapshot, "revision": revision, "computed_at": now.isoformat()}
        for entry in history.timeline:
            state = (
                connection.execute(
                    select(event_projections).where(
                        event_projections.c.record_id == entry.record_id
                    )
                )
                .mappings()
                .one()
            )
            if state["status"] != entry.projection_status or state["issues"] != list(entry.issues):
                connection.execute(
                    event_projections.update()
                    .where(event_projections.c.record_id == entry.record_id)
                    .values(
                        status=entry.projection_status,
                        issues=list(entry.issues),
                        revision=state["revision"] + 1,
                        updated_at=now,
                    )
                )
        return result

    @staticmethod
    def _view(row, timestamp: str) -> dict:
        return {
            **row["snapshot"],
            "revision": row["revision"],
            "computed_at": row[timestamp].isoformat(),
        }

    def get_history(self, item_id: str, *, revision: int | None = None) -> dict | None:
        """Read a derived snapshot; call replay to verify the underlying raw facts."""
        table = item_projections if revision is None else item_projection_versions
        query = select(table).where(table.c.item_id == item_id)
        if revision is not None:
            query = query.where(table.c.revision == revision)
        with self.engine.connect() as connection:
            row = connection.execute(query).mappings().one_or_none()
        return (
            None
            if row is None
            else self._view(row, "updated_at" if revision is None else "computed_at")
        )

    def replay(self, item_id: str) -> dict | None:
        """Verify all selected facts and rebuild. Unchanged replay creates no revision."""
        with self.engine.begin() as connection:
            production_guard(connection)
            if (
                connection.scalar(
                    select(quality_items.c.item_id).where(quality_items.c.item_id == item_id)
                )
                is not None
            ):
                raise InvalidTransition("Use the production journal for quality-managed replay.")
            _lock(connection, "item", item_id)
            facts = self._facts(connection, item_id)
            if not facts:
                return None
            return self._save(connection, rebuild_history(item_id, facts))
