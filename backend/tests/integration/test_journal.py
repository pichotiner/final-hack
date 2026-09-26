import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import event as sql_event
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.adapters.postgres import PostgresEventJournal, event_projections, metadata, raw_events
from app.application.ingestion import IngestEvent
from app.domain.errors import EventConflict, IntegrityViolation

pytestmark = pytest.mark.integration


@pytest.fixture
def prepared(database, protector, codec, event_document):
    admin, writer, _ = database
    event_document["source_id"] = f"source-{uuid4().hex}"
    raw = json.dumps(event_document, ensure_ascii=False).encode("utf-8")
    journal = PostgresEventJournal(writer)
    service = IngestEvent(journal, protector, codec)
    return admin, writer, journal, service, event_document, raw


def count_source(engine, table, source_id):
    with engine.connect() as connection:
        if table is raw_events:
            query = (
                select(func.count())
                .select_from(raw_events)
                .where(raw_events.c.source_id == source_id)
            )
        else:
            query = (
                select(func.count())
                .select_from(event_projections.join(raw_events))
                .where(raw_events.c.source_id == source_id)
            )
        return connection.scalar(query)


def test_migration_matches_storage_model(database):
    with database[0].connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), metadata) == []


def test_event_and_projection_are_committed_encrypted(prepared, protector):
    admin, _, journal, service, document, raw = prepared
    receipt = service.execute(raw, authenticated_source_id=document["source_id"])
    assert receipt.status == "accepted"
    assert receipt.projection_status == "unresolved"
    assert receipt.issues == ("domain_projection_not_implemented",)
    saved = journal.get(document["source_id"], document["event_id"])
    assert saved.metadata.envelope.occurred_at < receipt.received_at
    assert saved.protection.ciphertext != raw
    assert protector.open(saved.protection, saved.metadata) == raw
    assert count_source(admin, raw_events, document["source_id"]) == 1
    assert count_source(admin, event_projections, document["source_id"]) == 1


def test_duplicate_returns_original_receipt_after_projection_update(prepared, codec, protector):
    admin, _, journal, service, document, raw = prepared
    original = service.execute(raw, authenticated_source_id=document["source_id"])
    with admin.begin() as connection:
        connection.execute(
            event_projections.update()
            .where(event_projections.c.record_id == original.record_id)
            .values(status="applied", issues=[], revision=2)
        )
    later = IngestEvent(
        journal, protector, codec, clock=lambda: original.received_at + timedelta(days=1)
    )
    reordered = json.dumps(dict(reversed(list(document.items()))), indent=2).encode()
    duplicate = later.execute(reordered, authenticated_source_id=document["source_id"])
    assert duplicate.status == "duplicate"
    assert duplicate.record_id == original.record_id
    assert duplicate.received_at == original.received_at
    assert duplicate.projection_status == original.projection_status
    with admin.connect() as connection:
        assert (
            connection.scalar(
                select(event_projections.c.revision).where(
                    event_projections.c.record_id == original.record_id
                )
            )
            == 2
        )
    assert count_source(admin, raw_events, document["source_id"]) == 1


def test_conflicting_content_does_not_overwrite_evidence(prepared, protector):
    admin, _, journal, service, document, raw = prepared
    service.execute(raw, authenticated_source_id=document["source_id"])
    document["payload"]["observation_quality"] = "poor"
    with pytest.raises(EventConflict):
        service.execute(
            json.dumps(document).encode(), authenticated_source_id=document["source_id"]
        )
    stored = journal.get(document["source_id"], document["event_id"])
    assert protector.open(stored.protection, stored.metadata) == raw
    assert count_source(admin, raw_events, document["source_id"]) == 1


def test_same_event_id_from_different_sources_is_independent(prepared):
    admin, _, _, service, document, raw = prepared
    first = service.execute(raw, authenticated_source_id=document["source_id"])
    document["source_id"] += "-other"
    second = service.execute(
        json.dumps(document).encode(), authenticated_source_id=document["source_id"]
    )
    assert first.record_id != second.record_id
    assert count_source(admin, raw_events, document["source_id"]) == 1


def test_concurrent_duplicates_have_one_winner(prepared):
    admin, _, _, service, document, raw = prepared
    barrier = Barrier(8)

    def send(_):
        barrier.wait(timeout=10)
        return service.execute(raw, authenticated_source_id=document["source_id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(send, range(8)))
    assert sum(result.status == "accepted" for result in receipts) == 1
    assert sum(result.status == "duplicate" for result in receipts) == 7
    assert len({result.record_id for result in receipts}) == 1
    assert len({result.received_at for result in receipts}) == 1
    assert count_source(admin, raw_events, document["source_id"]) == 1
    assert count_source(admin, event_projections, document["source_id"]) == 1


def test_late_event_retains_both_times_and_is_not_lost(prepared):
    admin, _, journal, service, document, raw = prepared
    first = service.execute(raw, authenticated_source_id=document["source_id"])
    document["event_id"] = "late-event"
    document["occurred_at"] = "2026-09-24T09:00:00Z"
    late = service.execute(
        json.dumps(document).encode(), authenticated_source_id=document["source_id"]
    )
    stored = journal.get(document["source_id"], "late-event")
    assert stored.metadata.envelope.occurred_at == datetime(2026, 9, 24, 9, tzinfo=UTC)
    assert late.received_at >= first.received_at
    assert count_source(admin, raw_events, document["source_id"]) == 2
    assert late.projection_status == "unresolved"  # No false claim of a rebuilt product history.


def test_projection_failure_rolls_back_raw_event(prepared):
    admin, writer, _, service, document, raw = prepared

    def fail_projection(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO event_projections"):
            raise RuntimeError("Injected projection failure")

    sql_event.listen(writer, "before_cursor_execute", fail_projection)
    try:
        with pytest.raises(RuntimeError, match="Injected projection failure"):
            service.execute(raw, authenticated_source_id=document["source_id"])
    finally:
        sql_event.remove(writer, "before_cursor_execute", fail_projection)
    assert count_source(admin, raw_events, document["source_id"]) == 0
    assert count_source(admin, event_projections, document["source_id"]) == 0
    assert service.execute(raw, authenticated_source_id=document["source_id"]).status == "accepted"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE raw_events SET item_id = 'tampered' WHERE record_id = :id",
        "DELETE FROM raw_events WHERE record_id = :id",
        "TRUNCATE raw_events CASCADE",
    ],
)
def test_append_only_guards_block_modification_even_for_owner(prepared, statement):
    admin, _, _, service, document, raw = prepared
    receipt = service.execute(raw, authenticated_source_id=document["source_id"])
    with pytest.raises(DBAPIError) as error, admin.begin() as connection:
        connection.execute(text(statement), {"id": receipt.record_id})
    assert error.value.orig.sqlstate == "42501"
    assert count_source(admin, raw_events, document["source_id"]) == 1


def test_writer_cannot_disable_guards(prepared):
    _, writer, _, _, _, _ = prepared
    with pytest.raises(DBAPIError) as error, writer.begin() as connection:
        connection.execute(text("ALTER TABLE raw_events DISABLE TRIGGER USER"))
    assert error.value.orig.sqlstate == "42501"


def test_privileged_tampering_is_detected_before_duplicate_ack(prepared):
    admin, _, _, service, document, raw = prepared
    receipt = service.execute(raw, authenticated_source_id=document["source_id"])
    # Threat-model test: an owner bypasses the append-only trigger. AEAD must
    # still reject altered metadata instead of acknowledging an intact duplicate.
    with admin.begin() as connection:
        connection.execute(text("ALTER TABLE raw_events DISABLE TRIGGER USER"))
        connection.execute(
            raw_events.update()
            .where(raw_events.c.record_id == receipt.record_id)
            .values(item_id="tampered-item")
        )
        connection.execute(text("ALTER TABLE raw_events ENABLE TRIGGER USER"))
    with pytest.raises(IntegrityViolation):
        service.execute(raw, authenticated_source_id=document["source_id"])
