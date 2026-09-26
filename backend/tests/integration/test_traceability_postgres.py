import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.adapters.postgres import (
    PostgresEventJournal,
    event_projections,
    item_projection_versions,
    item_projections,
    raw_events,
)
from app.adapters.quality import PostgresQualityWorkflow, quality_commands, quality_outbox
from app.adapters.traceability import PostgresTraceabilityJournal
from app.application.ingestion import IngestEvent
from app.domain.errors import EventConflict, IntegrityViolation, InvalidEvent

pytestmark = pytest.mark.integration


@pytest.fixture
def trace(database, protector, codec, trace_document):
    admin, writer, _ = database
    journal = PostgresTraceabilityJournal(writer, protector)
    service = IngestEvent(journal, protector, codec)
    item_id, source_id = f"item-{uuid4().hex}", f"source-{uuid4().hex}"

    def document(event_id, kind="item_received", time="10:00:00", **changes):
        return trace_document(
            event_id, kind, time, **{"item_id": item_id, "source_id": source_id, **changes}
        )

    def send(doc):
        return service.execute(json.dumps(doc).encode(), authenticated_source_id=doc["source_id"])

    return admin, writer, journal, document, send, item_id


def count(engine, table, item_id):
    with engine.connect() as connection:
        return connection.scalar(
            select(func.count()).select_from(table).where(table.c.item_id == item_id)
        )


def test_two_operation_history_and_distinct_duration_origins(trace):
    admin, _, journal, document, send, item_id = trace
    receipt = send(document("received"))
    assert receipt.projection_status == "unresolved"
    assert receipt.issues == ("work_order_not_resolved",)
    send(document("incoming", "inspection_observed", "10:02:00"))
    for number, station, start, finish in [
        (1, "machining", "10:03:00", "10:05:00"),
        (2, "assembly", "10:07:00", "10:09:00"),
    ]:
        links = dict(
            operation_run_id=f"run-{number}",
            station_id=station,
            operator_id=f"operator-{number}",
            equipment_id=f"machine-{number}",
        )
        send(document(f"start-{number}", "operation_started", start, **links))
        finish_doc = document(f"finish-{number}", "operation_finished", finish, **links)
        finish_doc["payload"]["reported_duration"] = {
            "value": 80,
            "unit": "s",
            "kind": "active_processing",
        }
        assert send(finish_doc).projection_status == "applied"
    history = journal.get_history(item_id)
    assert history["revision"] == 6
    assert history["component_ids"] == ["bracket-001", "insert-001"]
    assert len(history["timeline"]) == 6
    assert [op["status"] for op in history["operations"]] == ["completed", "completed"]
    assert [op["durations"][0]["value"] for op in history["operations"]] == [120, 120]
    assert [op["durations"][1]["value"] for op in history["operations"]] == [80, 80]
    assert {op["operator_id"] for op in history["operations"]} == {"operator-1", "operator-2"}
    assert count(admin, raw_events, item_id) == 6
    assert count(admin, item_projection_versions, item_id) == 6


def test_late_start_rebuilds_finish_preserves_receipt_and_old_version(trace):
    admin, _, journal, document, send, item_id = trace
    send(document("received"))
    finish_doc = document("finish", "operation_finished", "10:05:00")
    original = send(finish_doc)
    old = journal.get_history(item_id)
    assert old["operations"][0]["status"] == "incomplete"
    assert old["operations"][0]["durations"][0]["value"] is None
    send(document("start", "operation_started", "10:03:00"))
    rebuilt = journal.get_history(item_id)
    assert rebuilt["revision"] == old["revision"] + 1
    assert rebuilt["operations"][0]["status"] == "completed"
    assert rebuilt["operations"][0]["durations"][0]["value"] == 120
    assert [entry["event_id"] for entry in rebuilt["timeline"]] == ["received", "start", "finish"]
    assert rebuilt["timeline"][1]["received_at"] >= rebuilt["timeline"][2]["received_at"]
    assert journal.get_history(item_id, revision=old["revision"]) == old
    duplicate = send(finish_doc)
    assert duplicate.status == "duplicate"
    assert duplicate.received_at == original.received_at
    assert duplicate.projection_status == original.projection_status == "unresolved"
    assert duplicate.issues == original.issues == ("missing_start",)
    assert journal.get_history(item_id) == rebuilt
    with admin.connect() as connection:
        state = (
            connection.execute(
                select(event_projections).where(event_projections.c.record_id == original.record_id)
            )
            .mappings()
            .one()
        )
        assert state["status"] == "applied"
        assert state["revision"] == 2


def test_late_receipt_resolves_operation_identity_without_fabricating_passport(trace):
    _, _, journal, document, send, item_id = trace
    first = send(document("start", "operation_started", "10:03:00"))
    assert first.issues == ("item_receipt_missing",)
    assert journal.get_history(item_id)["item_type_id"] is None
    send(document("received"))
    history = journal.get_history(item_id)
    assert history["item_type_id"] == "bracket-demo"
    assert history["timeline"][-1]["projection_status"] == "applied"


def test_eight_concurrent_deliveries_create_one_history_version(trace):
    admin, _, journal, document, send, item_id = trace
    barrier = Barrier(8)
    doc = document("received")

    def deliver(_):
        barrier.wait(timeout=10)
        return send(doc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(deliver, range(8)))
    assert sum(r.status == "accepted" for r in receipts) == 1
    assert len({r.record_id for r in receipts}) == 1
    assert journal.get_history(item_id)["revision"] == 1
    assert count(admin, raw_events, item_id) == 1
    assert count(admin, item_projection_versions, item_id) == 1


def test_concurrent_different_events_preserve_entire_item_history(trace):
    _, _, journal, document, send, item_id = trace
    send(document("received"))
    docs = [
        document(f"{kind}-{n}", kind, time, operation_run_id=f"run-{n}")
        for n in range(4)
        for kind, time in [("operation_started", "10:03:00"), ("operation_finished", "10:05:00")]
    ]
    barrier = Barrier(8)

    def deliver(doc):
        barrier.wait(timeout=10)
        return send(doc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(deliver, reversed(docs)))
    assert all(r.status == "accepted" for r in receipts)
    history = journal.get_history(item_id)
    assert history["revision"] == len(history["timeline"]) == 9
    assert len(history["operations"]) == 4
    assert all(op["status"] == "completed" for op in history["operations"])
    assert all(op["durations"][0]["value"] == 120 for op in history["operations"])


def test_same_event_id_different_sources_remain_distinct_evidence(trace):
    _, _, journal, document, send, item_id = trace
    send(document("received"))
    send(document("observation", "inspection_observed", "10:02:00", source_id="vision-a"))
    send(document("observation", "inspection_observed", "10:02:00", source_id="vision-b"))
    timeline = journal.get_history(item_id)["timeline"]
    observations = [entry for entry in timeline if entry["event_id"] == "observation"]
    assert [entry["source_id"] for entry in observations] == ["vision-a", "vision-b"]
    assert len({entry["record_id"] for entry in observations}) == 2


def test_traceability_rejects_incompatible_isolation(trace, protector):
    _, writer, _, _, _, _ = trace
    incompatible = writer.execution_options(isolation_level="REPEATABLE READ")
    with pytest.raises(ValueError, match="READ COMMITTED"):
        PostgresTraceabilityJournal(incompatible, protector)


def test_conflict_cannot_change_history_even_when_different_item_is_claimed(trace):
    admin, _, journal, document, send, item_id = trace
    doc = document("received")
    send(doc)
    original = journal.get_history(item_id)
    doc["item_id"] = f"other-{uuid4().hex}"
    with pytest.raises(EventConflict):
        send(doc)
    assert journal.get_history(item_id) == original
    assert journal.get_history(doc["item_id"]) is None
    assert count(admin, item_projection_versions, item_id) == 1


@pytest.mark.parametrize("target", ["item_projections", "item_projection_versions"])
def test_history_failure_rolls_back_raw_current_state_and_versions(trace, target):
    admin, writer, journal, document, send, item_id = trace
    send(document("received"))
    original = journal.get_history(item_id)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"INSERT INTO {target} "):
            raise RuntimeError("Injected history failure")

    sql_event.listen(writer, "before_cursor_execute", fail)
    doc = document("start", "operation_started", "10:03:00")
    try:
        with pytest.raises(RuntimeError, match="Injected history failure"):
            send(doc)
    finally:
        sql_event.remove(writer, "before_cursor_execute", fail)
    assert journal.get_history(item_id) == original
    assert count(admin, raw_events, item_id) == count(admin, item_projection_versions, item_id) == 1
    assert journal.get(doc["source_id"], doc["event_id"]) is None
    assert send(doc).status == "accepted"


def test_invalid_domain_input_is_not_partially_committed(trace):
    admin, _, journal, document, send, item_id = trace
    doc = document("received")
    del doc["payload"]["assembly_version"]
    with pytest.raises(InvalidEvent):
        send(doc)
    assert journal.get_history(item_id) is None
    assert count(admin, raw_events, item_id) == 0


@pytest.mark.parametrize("item_unknown", [False, True])
def test_uncertain_identification_is_saved_but_not_attached(trace, item_unknown):
    admin, _, journal, document, send, item_id = trace
    send(document("received"))
    before = journal.get_history(item_id)
    doc = document("uncertain", "operation_started", identification_status="uncertain")
    if item_unknown:
        doc["item_id"] = None
    result = send(doc)
    assert result.issues == ("unreliable_item_identification",)
    assert journal.get(doc["source_id"], "uncertain") is not None
    assert journal.get_history(item_id) == before
    assert count(admin, item_projection_versions, item_id) == 1


def test_explicit_replay_of_legacy_records_and_repeated_replay(trace, protector, codec):
    admin, writer, journal, document, _, item_id = trace
    service = IngestEvent(PostgresEventJournal(writer), protector, codec)
    for doc in [
        document("received"),
        document("start", "operation_started", "10:03:00"),
        document("finish", "operation_finished", "10:05:00"),
    ]:
        service.execute(json.dumps(doc).encode(), authenticated_source_id=doc["source_id"])
    assert journal.get_history(item_id) is None
    history = journal.replay(item_id)
    assert history["revision"] == 1
    assert history["operations"][0]["durations"][0]["value"] == 120
    assert journal.replay(item_id) == history
    assert count(admin, item_projection_versions, item_id) == 1
    assert journal.replay(f"missing-{uuid4().hex}") is None


def test_replay_repairs_mutable_projection_from_authenticated_facts(trace):
    admin, _, journal, document, send, item_id = trace
    send(document("received"))
    before = journal.get_history(item_id)
    with admin.begin() as connection:
        connection.execute(
            item_projections.update()
            .where(item_projections.c.item_id == item_id)
            .values(snapshot={"item_id": item_id, "component_ids": ["fake"]})
        )
    restored = journal.replay(item_id)
    assert restored["component_ids"] == before["component_ids"]
    assert restored["revision"] == 2
    assert journal.get_history(item_id, revision=1) == before


def test_tampered_fact_blocks_new_ingestion_and_replay(trace):
    admin, _, journal, document, send, item_id = trace
    received = send(document("received"))
    original = journal.get_history(item_id)
    # Privileged corruption of ciphertext without changing query selectors.
    with admin.begin() as connection:
        connection.execute(text("ALTER TABLE raw_events DISABLE TRIGGER USER"))
        connection.execute(
            raw_events.update()
            .where(raw_events.c.record_id == received.record_id)
            .values(ciphertext=b"x" * 32)
        )
        connection.execute(text("ALTER TABLE raw_events ENABLE TRIGGER USER"))
    with pytest.raises(IntegrityViolation):
        send(document("start", "operation_started"))
    with pytest.raises(IntegrityViolation):
        journal.replay(item_id)
    assert journal.get_history(item_id) == original
    assert count(admin, raw_events, item_id) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE item_projection_versions SET snapshot = '{}' WHERE item_id = :item",
        "DELETE FROM item_projection_versions WHERE item_id = :item",
        "TRUNCATE item_projection_versions",
    ],
)
def test_old_versions_are_append_only_even_for_owner(trace, statement):
    admin, _, journal, document, send, item_id = trace
    send(document("received"))
    with pytest.raises(DBAPIError) as error, admin.begin() as connection:
        connection.execute(text(statement), {"item": item_id})
    assert error.value.orig.sqlstate == "42501"
    assert journal.get_history(item_id, revision=1) is not None


def test_quality_workflow_persists_basis_decision_and_idempotent_outbox(trace):
    admin, writer, _, _, _, _ = trace
    workflow = PostgresQualityWorkflow(writer)
    item_id = f"quality-{uuid4().hex}"
    workflow.open_item(item_id, required_checkpoints=("incoming", "final"))
    workflow.record_observation(
        item_id,
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        item_id,
        observation_id="defect-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    state = workflow.state(item_id)
    nonconformity_id = state["nonconformities"][0]["nonconformity_id"]
    basis = workflow.create_basis(item_id, target_id=nonconformity_id)
    command = dict(
        action="confirm",
        reason="visible mark",
        actor_id="inspector-1",
        expected_revision=basis["context_revision"],
        expected_basis_id=basis["basis_id"],
        idempotency_key="confirm-1",
    )
    first = workflow.decide_nonconformity(nonconformity_id, **command)
    second = workflow.decide_nonconformity(nonconformity_id, **command)
    assert second == first
    with admin.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(quality_commands)
                .where(quality_commands.c.target_id == nonconformity_id)
            )
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(quality_outbox)
                .where(quality_outbox.c.item_id == item_id)
            )
            == 3
        )


def test_quality_workflow_persists_completed_rework_and_release_kpi(trace):
    admin, writer, _, _, _, _ = trace
    workflow = PostgresQualityWorkflow(writer)
    item_id = f"release-{uuid4().hex}"
    workflow.open_item(item_id, required_checkpoints=("final",))
    workflow.record_observation(
        item_id,
        observation_id="defect-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    nonconformity_id = workflow.state(item_id)["nonconformities"][0]["nonconformity_id"]
    current = workflow.create_basis(item_id, target_id=nonconformity_id)
    workflow.decide_nonconformity(
        nonconformity_id,
        action="confirm",
        reason="visible mark",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="confirm-1",
    )
    current = workflow.create_basis(item_id)
    workflow.dispose(
        item_id,
        action="rework",
        reason="replace marked part",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="rework-1",
    )
    current = workflow.create_basis(item_id)
    workflow.start_cycle(
        item_id,
        cycle_id="cycle-2",
        actor_id="master-1",
        reason="assigned rework",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="cycle-2",
    )
    workflow.record_rework_operation(
        item_id,
        operation_id="rework-run-2",
        previous_operation_run_id="run-1",
        cycle_id="cycle-2",
    )
    workflow.record_observation(
        item_id,
        observation_id="final-2",
        checkpoint_id="final",
        result="no_defect_signs",
        cycle_id="cycle-2",
    )
    current = workflow.create_basis(item_id, target_id=nonconformity_id)
    workflow.resolve_nonconformity(
        nonconformity_id,
        reason="repeat control passed",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="resolve-1",
        rework_operation_id="rework-run-2",
        previous_operation_run_id="run-1",
        observation_ids=("final-2",),
    )
    current = workflow.create_basis(item_id)
    workflow.dispose(
        item_id,
        action="released",
        reason="all checks passed after rework",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="release-1",
    )
    state = PostgresQualityWorkflow(writer).state(item_id)
    assert state["release_allowed_now"] is True
    assert state["kpi"]["confirmed_defects"] == 1
    assert state["kpi"]["resolved_confirmed_defects"] == 1
    assert state["kpi"]["rework_operations"] == 1
    with admin.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(quality_outbox)
                .where(quality_outbox.c.item_id == item_id)
            )
            == state["context_revision"]
        )
