import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
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
    quality_bases,
    quality_context_versions,
    quality_items,
    raw_events,
)
from app.adapters.production import PostgresProductionJournal
from app.adapters.quality import PostgresQualityWorkflow, quality_commands, quality_outbox
from app.adapters.traceability import PostgresTraceabilityJournal
from app.application.ingestion import IngestEvent
from app.application.production import IdentityDirectory, Principal, ProductionService
from app.domain.errors import (
    AuthenticationRequired,
    EventConflict,
    IdempotencyConflict,
    IntegrityViolation,
    InvalidEvent,
    InvalidTransition,
    PermissionDenied,
)
from app.domain.quality import InMemoryErpReceiver
from tests.quality_support import Rig, context

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


@pytest.fixture
def production(database, protector, codec, trace_document):
    admin, writer, _ = database
    journal = PostgresProductionJournal(writer, protector)
    item_id = f"production-{uuid4().hex}"
    source_id = f"vision-{uuid4().hex}"
    sources = {
        "item_received": f"erp-{uuid4().hex}",
        "operation_started": f"mes-{uuid4().hex}",
        "operation_finished": f"mes-{uuid4().hex}",
        "inspection_observed": source_id,
        "operator_action": f"operator-terminal-{uuid4().hex}",
        "machine_state": f"machine-source-{uuid4().hex}",
    }
    sources["operation_finished"] = sources["operation_started"]
    production_context = context(item_id)
    production_context = production_context.__class__(
        **{
            **production_context.__dict__,
            "work_order_id": "order-001",
            "assembly_version": "1",
            "component_ids": ("bracket-001", "insert-001"),
            "controls": tuple(
                rule.__class__(
                    checkpoint_id=rule.checkpoint_id,
                    source_id=source_id,
                    analyzer_version="analyzer-1",
                    operation_run_id="run-2" if rule.checkpoint_id == "final" else None,
                    inspection_phase="final" if rule.checkpoint_id == "final" else "incoming",
                    control_session_id=f"cycle-1-{rule.checkpoint_id}",
                )
                for rule in production_context.controls
            ),
            "source_authorities": (
                type(production_context.source_authorities[0])(
                    sources["item_received"], ("item_received",)
                ),
                type(production_context.source_authorities[0])(
                    sources["operation_started"],
                    ("operation_started", "operation_finished"),
                ),
                type(production_context.source_authorities[0])(
                    sources["inspection_observed"], ("inspection_observed",)
                ),
                type(production_context.source_authorities[0])(
                    sources["operator_action"], ("operator_action",)
                ),
                type(production_context.source_authorities[0])(
                    sources["machine_state"], ("machine_state",)
                ),
            ),
        }
    )
    journal.import_context(
        item_id, production_context, import_id=f"import-{item_id}", actor_id="erp-adapter"
    )
    service = IngestEvent(journal, protector, codec)

    def document(event_id, kind="item_received", time="10:00:00", **changes):
        return trace_document(
            event_id,
            kind,
            time,
            **{"item_id": item_id, "source_id": sources[kind], **changes},
        )

    def send(document):
        return service.execute(
            json.dumps(document).encode(), authenticated_source_id=document["source_id"]
        )

    return admin, writer, journal, document, send, item_id, source_id, production_context


def normal_production(production):
    _, _, _, document, send, _, _, _ = production
    send(document("received"))
    incoming = document("incoming", "inspection_observed", "10:02:00")
    incoming["payload"].update(
        analyzer_version="analyzer-1",
        control_cycle_id="cycle-1",
        control_session_id="cycle-1-incoming",
        checkpoint_id="incoming",
    )
    send(incoming)
    for number, station, start, finish in [
        (1, "machining", "10:03:00", "10:05:00"),
        (2, "assembly", "10:07:00", "10:09:00"),
    ]:
        links = {
            "operation_run_id": f"run-{number}",
            "station_id": station,
            "operator_id": f"operator-{number}",
            "equipment_id": f"machine-{number}",
        }
        send(document(f"start-{number}", "operation_started", start, **links))
        send(document(f"finish-{number}", "operation_finished", finish, **links))


def final_inspection(production, event_id="final", time="10:10:00", *, defect=False):
    _, _, _, document, send, _, _, _ = production
    result = document(
        event_id,
        "inspection_observed",
        time,
        station_id="final",
        operation_run_id="run-2",
    )
    result["payload"].update(
        checkpoint_id="final",
        inspection_phase="final",
        analyzer_version="analyzer-1",
        control_cycle_id="cycle-1",
        control_session_id="cycle-1-final",
    )
    if defect:
        result["payload"].update(
            inspection_result="defect_signs",
            defects=[
                {
                    "defect_ref": "mark-1",
                    "type": "surface-mark",
                    "component_id": None,
                    "region_id": None,
                }
            ],
        )
    return send(result)


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
    from tests.quality_support import Rig

    admin, writer, _, _, _, _ = trace
    workflow = PostgresQualityWorkflow(writer)
    item_id = f"quality-{uuid4().hex}"
    rig = Rig(workflow, item_id)
    rig.observe("incoming-1", "incoming")
    rig.defect()
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
            == 4
        )


def test_quality_workflow_persists_completed_rework_and_release_kpi(trace):
    from tests.quality_support import Rig

    admin, writer, _, _, _, _ = trace
    workflow = PostgresQualityWorkflow(writer)
    item_id = f"release-{uuid4().hex}"
    rig = Rig(workflow, item_id, checkpoints=("final",))
    rig.defect()
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
        operation_run_ids=("rework-1",),
        control_sessions={"final": "cycle-2-final"},
        checkpoint_operation_run_ids={"final": "rework-1"},
    )
    rig.complete_rework()
    rig.observe("final-2")
    current = workflow.create_basis(item_id, target_id=nonconformity_id)
    workflow.resolve_nonconformity(
        nonconformity_id,
        reason="repeat control passed",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="resolve-1",
        rework_operation_id="rework-1",
        previous_operation_run_id="run-2",
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
    assert state["blockers"] == ()
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


def test_production_journal_atomically_projects_raw_quality_basis_and_outbox(production):
    admin, _, journal, _, _, item_id, _, _ = production
    normal_production(production)
    receipt = final_inspection(production)
    assert receipt.projection_status == "applied"
    state = journal.quality.state(item_id)
    assert state["blockers"] == ()
    assert state["release_allowed_now"] is True
    assert state["kpi"]["inspected_items"] == 1
    assert len(state["observations"]) == 2
    assert len(state["outbox"]) == state["context_revision"] == 8
    assert count(admin, raw_events, item_id) == 7
    # P7 prepares the initial revision-zero B on write, plus B for revisions 1..8.
    assert count(admin, quality_bases, item_id) == 9
    history = journal.get_history(item_id)
    assert len(history["timeline"]) == 7
    assert {entry["projection_status"] for entry in history["timeline"]} == {"applied"}
    with admin.connect() as connection:
        basis_id = connection.scalar(
            select(quality_bases.c.basis_id)
            .where(quality_bases.c.item_id == item_id)
            .order_by(quality_bases.c.created_at.desc())
        )
    basis = journal.quality.get_basis(item_id, basis_id)
    assert basis["context_snapshot"]["production_context"]["route_version"] == "route-1"


def test_production_invalid_payload_rolls_back_raw_quality_and_outbox(production):
    admin, _, journal, document, send, item_id, _, _ = production
    before = journal.quality.state(item_id)
    invalid = document("bad", "inspection_observed", "10:01:00")
    invalid["payload"].update(
        inspection_result="defect_signs",
        defects=[],
        analyzer_version="analyzer-1",
        control_cycle_id="cycle-1",
    )
    with pytest.raises(InvalidEvent):
        send(invalid)
    assert count(admin, raw_events, item_id) == 0
    assert journal.quality.state(item_id) == before


def test_source_cycle_claim_without_registered_session_stays_unresolved(production):
    _, _, journal, document, send, item_id, _, _ = production
    send(document("received"))
    claimed = document("claimed-cycle", "inspection_observed", "10:02:00")
    claimed["payload"].update(
        checkpoint_id="incoming",
        inspection_phase="incoming",
        analyzer_version="analyzer-1",
        control_cycle_id="cycle-1",
        control_session_id="source-chosen-session",
    )
    receipt = send(claimed)
    assert receipt.projection_status == "unresolved"
    state = journal.quality.state(item_id)
    assert state["observations"][0]["cycle_id"] is None
    assert "observation_cycle_unresolved" in state["blockers"]
    assert "missing_checkpoint:incoming" in state["blockers"]


def test_production_storage_failure_rolls_back_quality_basis_and_outbox(production):
    admin, writer, journal, document, send, item_id, _, _ = production
    before = journal.quality.state(item_id)
    before_bases = count(admin, quality_bases, item_id)
    before_outbox = count(admin, quality_outbox, item_id)

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO raw_events "):
            raise RuntimeError("Injected raw storage failure after quality projection")

    sql_event.listen(writer, "before_cursor_execute", fail)
    event = document("received-failure")
    try:
        with pytest.raises(RuntimeError, match="Injected raw storage failure"):
            send(event)
    finally:
        sql_event.remove(writer, "before_cursor_execute", fail)
    assert count(admin, raw_events, item_id) == 0
    assert count(admin, quality_bases, item_id) == before_bases
    assert count(admin, quality_outbox, item_id) == before_outbox
    assert journal.quality.state(item_id) == before
    assert send(event).status == "accepted"


def test_production_late_defect_preserves_decision_and_one_logical_defect(production):
    _, _, journal, _, _, item_id, _, _ = production
    normal_production(production)
    final_inspection(production, defect=True)
    state = journal.quality.state(item_id)
    nonconformity_id = state["nonconformities"][0]["nonconformity_id"]
    basis = journal.quality.create_basis(item_id, nonconformity_id)
    decision = journal.quality.decide_nonconformity(
        nonconformity_id,
        action="confirm",
        reason="visible synthetic mark",
        actor_id="controller-1",
        expected_revision=basis["context_revision"],
        expected_basis_id=basis["basis_id"],
        idempotency_key="confirm-1",
    )
    final_inspection(production, "late", "10:02:30", defect=True)
    after = journal.quality.state(item_id)
    assert len(after["nonconformities"]) == 1
    assert after["kpi"]["confirmed_defects"] == 1
    assert after["review_required"] is True
    assert decision["decision_id"] in after["nonconformities"][0]["decision_ids"]
    old = journal.quality.get_basis(item_id, basis["basis_id"])
    assert not any(value.endswith(":0") and "late" in value for value in old["observation_ids"])


def test_operator_cause_requires_exact_reviewed_action_and_does_not_follow_chronology(
    production,
):
    _, _, journal, document, send, item_id, _, _ = production
    normal_production(production)
    action = document(
        "action-1",
        "operator_action",
        "10:08:30",
        operation_run_id="run-2",
        station_id="assembly",
        operator_id="operator-2",
    )
    send(action)
    machine = document(
        "machine-warning",
        "machine_state",
        "10:08:40",
        operation_run_id="run-2",
        station_id="assembly",
        equipment_id="machine-2",
    )
    send(machine)
    final_inspection(production, defect=True)
    state = journal.quality.state(item_id)
    nc = state["nonconformities"][0]["nonconformity_id"]
    assert state["nonconformities"][0]["cause_status"] == "unresolved"
    assert state["kpi"]["confirmed_operator_errors"] == 0
    history = journal.get_history(item_id)
    action_entry = next(entry for entry in history["timeline"] if entry["event_id"] == "action-1")
    machine_entry = next(
        entry for entry in history["timeline"] if entry["event_id"] == "machine-warning"
    )
    assert action_entry["details"]["action_type"] == "mode-confirmed"
    assert machine_entry["details"]["state"] == "warning"
    basis = journal.quality.create_basis(item_id, nc)
    with pytest.raises(InvalidTransition):
        journal.quality.decide_nonconformity(
            nc,
            action="confirm",
            reason="chronology alone is insufficient",
            actor_id="controller-1",
            expected_revision=basis["context_revision"],
            expected_basis_id=basis["basis_id"],
            idempotency_key="bad-cause",
            cause_status="established",
            cause_category="operator",
            cause_evidence_record_ids=(str(machine_entry["record_id"]),),
            operation_run_id="run-2",
            operator_id="operator-2",
            action_record_id=str(action_entry["record_id"]),
        )
    decision = journal.quality.decide_nonconformity(
        nc,
        action="confirm",
        reason="explicit reviewed action and operation evidence",
        actor_id="controller-1",
        expected_revision=basis["context_revision"],
        expected_basis_id=basis["basis_id"],
        idempotency_key="established-cause",
        cause_status="established",
        cause_category="operator",
        cause_evidence_record_ids=(str(action_entry["record_id"]),),
        operation_run_id="run-2",
        operator_id="operator-2",
        action_record_id=str(action_entry["record_id"]),
    )
    assert decision["cause_status"] == "established"
    assert journal.quality.state(item_id)["kpi"]["confirmed_operator_errors"] == 1


def test_legacy_writers_are_rejected_for_quality_managed_item(production, protector, codec):
    _, writer, _, document, _, _, _, _ = production
    legacy = IngestEvent(PostgresTraceabilityJournal(writer, protector), protector, codec)
    doc = document("legacy")
    with pytest.raises(InvalidTransition):
        legacy.execute(json.dumps(doc).encode(), authenticated_source_id=doc["source_id"])
    raw_only = IngestEvent(PostgresEventJournal(writer), protector, codec)
    with pytest.raises(InvalidTransition):
        raw_only.execute(json.dumps(doc).encode(), authenticated_source_id=doc["source_id"])


def test_context_import_is_idempotent_and_historical(production):
    admin, _, journal, _, _, item_id, _, context_value = production
    duplicate = journal.import_context(
        item_id,
        context_value,
        import_id=f"import-{item_id}",
        actor_id="erp-adapter",
    )
    assert duplicate["status"] == "duplicate"
    changed = context_value.__class__(**{**context_value.__dict__, "route_version": "route-2"})
    with pytest.raises(IdempotencyConflict):
        journal.import_context(
            item_id,
            changed,
            import_id=f"import-{item_id}",
            actor_id="erp-adapter",
        )
    with admin.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(quality_context_versions)) >= 1


def test_precontext_legacy_facts_are_backfilled_before_quality_release(
    database, protector, codec, trace_document
):
    admin, writer, _ = database
    item_id = f"backfill-{uuid4().hex}"
    legacy_journal = PostgresTraceabilityJournal(writer, protector)
    legacy = IngestEvent(legacy_journal, protector, codec)

    def send(kind, event_id, time, source, **changes):
        document = trace_document(
            event_id,
            kind,
            time,
            **{"item_id": item_id, "source_id": source, **changes},
        )
        if kind == "inspection_observed":
            document["payload"].update(
                checkpoint_id="incoming" if event_id == "incoming" else "final",
                inspection_phase="incoming" if event_id == "incoming" else "final",
                analyzer_version="analyzer-1",
                control_session_id=(
                    "cycle-1-incoming" if event_id == "incoming" else "cycle-1-final"
                ),
            )
        return document, legacy.execute(
            json.dumps(document).encode(), authenticated_source_id=document["source_id"]
        )

    received, original = send("item_received", "received", "10:00:00", "erp-source")
    send("inspection_observed", "incoming", "10:02:00", "vision-1")
    for number, start, finish in ((1, "10:03:00", "10:05:00"), (2, "10:07:00", "10:09:00")):
        links = {"operation_run_id": f"run-{number}", "station_id": f"station-{number}"}
        send("operation_started", f"start-{number}", start, "mes-source", **links)
        send("operation_finished", f"finish-{number}", finish, "mes-source", **links)
    send(
        "inspection_observed",
        "final",
        "10:10:00",
        "vision-1",
        station_id="final",
        operation_run_id="run-2",
    )
    assert count(admin, quality_items, item_id) == 0
    journal = PostgresProductionJournal(writer, protector)
    value = context(item_id)
    value = value.__class__(
        **{
            **value.__dict__,
            "work_order_id": "order-001",
            "assembly_version": "1",
            "component_ids": ("bracket-001", "insert-001"),
        }
    )
    journal.import_context(
        item_id, value, import_id=f"backfill-import-{item_id}", actor_id="erp-adapter"
    )
    state = journal.quality.state(item_id)
    assert state["blockers"] == ()
    assert len(state["observations"]) == 2
    assert state["context_revision"] == 8
    production_service = IngestEvent(journal, protector, codec)
    duplicate = production_service.execute(
        json.dumps(received).encode(), authenticated_source_id="erp-source"
    )
    assert duplicate.status == "duplicate"
    assert duplicate.received_at == original.received_at
    assert journal.quality.state(item_id) == state


def test_managed_item_rejects_source_outside_event_authority(production):
    admin, _, journal, document, send, item_id, vision_source, _ = production
    before = journal.quality.state(item_id)
    forged = document(
        "forged-operation",
        "operation_started",
        "10:01:00",
        source_id=vision_source,
        operation_run_id="run-1",
    )
    with pytest.raises(PermissionDenied):
        send(forged)
    assert count(admin, raw_events, item_id) == 0
    assert journal.quality.state(item_id) == before


def test_external_stream_is_unique_and_cannot_be_remapped(production):
    _, _, journal, _, _, item_id, _, value = production
    other_item = f"other-{uuid4().hex}"
    duplicate_stream = value.__class__(
        **{**value.__dict__, "external_item_id": value.external_item_id}
    )
    with pytest.raises(InvalidTransition):
        journal.import_context(
            other_item,
            duplicate_stream,
            import_id=f"duplicate-stream-{other_item}",
            actor_id="erp-adapter",
        )
    remapped = value.__class__(**{**value.__dict__, "external_item_id": "changed-external-item"})
    with pytest.raises(InvalidTransition):
        journal.import_context(
            item_id,
            remapped,
            import_id=f"remap-{item_id}",
            actor_id="erp-adapter",
        )


def test_unknown_item_opens_constraint_for_future_items(database, protector, codec, event_document):
    _, writer, _ = database
    journal = PostgresProductionJournal(writer, protector)
    service = IngestEvent(journal, protector, codec)
    unknown = dict(event_document)
    unknown["event_id"] = f"unknown-{uuid4().hex}"
    unknown["item_id"] = None
    unknown["identification_status"] = "uncertain"
    unknown["payload"] = {
        **unknown["payload"],
        "analyzer_version": "analyzer-1",
        "control_session_id": "unknown-session",
    }
    receipt = service.execute(json.dumps(unknown).encode(), authenticated_source_id="vision-01")
    assert receipt.projection_status == "unresolved"
    future = f"future-{uuid4().hex}"
    journal.import_context(
        future, context(future), import_id=f"import-{future}", actor_id="erp-adapter"
    )
    state = journal.quality.state(future)
    assert any(value.startswith("scope_constraint:") for value in state["blockers"])


def test_scope_resolution_uses_current_bases_and_clears_only_one_reason(
    database, protector, codec, event_document
):
    _, writer, _ = database
    journal = PostgresProductionJournal(writer, protector)
    service = IngestEvent(journal, protector, codec)
    constraint_ids = []
    for number in (1, 2):
        unknown = dict(event_document)
        unknown["event_id"] = f"scope-{number}-{uuid4().hex}"
        unknown["item_id"] = None
        unknown["identification_status"] = "uncertain"
        unknown["payload"] = {
            **unknown["payload"],
            "analyzer_version": "analyzer-1",
            "control_session_id": f"unknown-session-{number}",
        }
        receipt = service.execute(json.dumps(unknown).encode(), authenticated_source_id="vision-01")
        constraint_ids.append(str(receipt.record_id))
    item_ids = []
    with writer.connect() as connection:
        item_ids = connection.scalars(select(quality_items.c.item_id)).all()
    bases = {}
    for item_id in item_ids:
        basis = journal.quality.create_basis(item_id)
        bases[item_id] = {
            "basis_id": basis["basis_id"],
            "context_revision": basis["context_revision"],
        }
    result = journal.resolve_scope(
        constraint_ids[0],
        actor_id="controller-1",
        reason="physical reconciliation completed for this one cause",
        idempotency_key="resolve-scope-1",
        bases=bases,
    )
    assert result["constraint_id"] == constraint_ids[0]
    assert (
        journal.resolve_scope(
            constraint_ids[0],
            actor_id="controller-1",
            reason="physical reconciliation completed for this one cause",
            idempotency_key="resolve-scope-1",
            bases=bases,
        )
        == result
    )
    with pytest.raises(IdempotencyConflict):
        journal.resolve_scope(
            constraint_ids[0],
            actor_id="controller-1",
            reason="changed reason",
            idempotency_key="resolve-scope-1",
            bases=bases,
        )
    sample = journal.quality.state(item_ids[-1])
    assert f"scope_constraint:{constraint_ids[0]}" not in sample["blockers"]
    assert f"scope_constraint:{constraint_ids[1]}" in sample["blockers"]


def test_production_global_guard_preserves_parallel_events_and_duplicates(production):
    admin, _, journal, document, send, item_id, _, _ = production
    send(document("received"))
    documents = [
        document(
            f"machine-{number}",
            "machine_state",
            f"10:0{number}:00",
            operation_run_id="run-1",
            equipment_id="machine-1",
        )
        for number in range(1, 9)
    ]
    barrier = Barrier(8)

    def concurrent_send(value):
        barrier.wait()
        return send(value)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(concurrent_send, documents))
    assert {receipt.status for receipt in receipts} == {"accepted"}
    state = journal.quality.state(item_id)
    assert state["context_revision"] == 10
    assert len(state["outbox"]) == 10
    assert count(admin, raw_events, item_id) == 9
    with ThreadPoolExecutor(max_workers=8) as pool:
        duplicates = list(pool.map(send, documents))
    assert {receipt.status for receipt in duplicates} == {"duplicate"}
    assert journal.quality.state(item_id) == state


def test_postgres_sender_persists_full_ack_and_reuses_original_receipt(trace):
    _, writer, _, _, _, _ = trace
    rig = Rig(PostgresQualityWorkflow(writer), f"ack-{uuid4().hex}", checkpoints=("final",))
    rig.observe("clean")
    message = SimpleNamespace(**rig.state()["outbox"][-1])
    receiver = InMemoryErpReceiver()
    accepted = receiver.receive(message)
    assert rig.workflow.acknowledge(message.message_id, receipt=accepted) == accepted
    duplicate = receiver.receive(message)
    assert duplicate["status"] == "duplicate"
    assert rig.workflow.acknowledge(message.message_id, receipt=duplicate) == accepted
    restored = PostgresQualityWorkflow(writer).state(rig.item_id)
    saved = next(value for value in restored["outbox"] if value["message_id"] == message.message_id)
    assert saved["status"] == "acknowledged"
    assert saved["ack"] == accepted


def test_authenticated_service_uses_server_actor_and_roles(production, protector):
    _, _, journal, _, _, item_id, source_id, _ = production
    tokens = {
        "source-token": Principal("camera-service", frozenset({"source"}), source_id),
        "controller-token": Principal("controller-7", frozenset({"controller"})),
        "master-token": Principal("master-2", frozenset({"master"})),
    }
    directory = IdentityDirectory(
        {
            hashlib.sha256(token.encode()).hexdigest(): principal
            for token, principal in tokens.items()
        }
    )
    service = ProductionService(journal, protector, directory)
    normal_production(production)
    final_inspection(production, defect=True)
    nc = journal.quality.state(item_id)["nonconformities"][0]["nonconformity_id"]
    basis = service.create_basis("controller-token", item_id, nc)
    result = service.command(
        "controller-token",
        "decide_nonconformity",
        nc,
        {
            "action": "confirm",
            "reason": "reviewed by authenticated controller",
            "expected_revision": basis["context_revision"],
            "expected_basis_id": basis["basis_id"],
            "idempotency_key": "auth-confirm",
        },
    )
    assert result["actor_id"] == "controller-7"
    with pytest.raises(PermissionDenied):
        service.command(
            "master-token",
            "decide_nonconformity",
            nc,
            {
                "action": "reject",
                "reason": "not allowed",
                "expected_revision": result["context_revision"],
                "expected_basis_id": basis["basis_id"],
                "idempotency_key": "bad-role",
            },
        )
    with pytest.raises(InvalidEvent):
        service.command("controller-token", "dispose", item_id, {"actor_id": "forged"})
    with pytest.raises(AuthenticationRequired):
        service.state("wrong-token", item_id)


def test_outbox_body_and_history_tables_reject_mutation(trace):
    admin, writer, _, _, _, _ = trace
    rig = Rig(PostgresQualityWorkflow(writer), f"immutable-{uuid4().hex}", checkpoints=("final",))
    rig.observe("clean")
    message_id = rig.state()["outbox"][-1]["message_id"]
    with pytest.raises(DBAPIError) as error:
        with admin.begin() as connection:
            connection.execute(
                text("UPDATE quality_outbox SET payload = '{}'::jsonb WHERE message_id = :message"),
                {"message": message_id},
            )
    assert error.value.orig.sqlstate == "42501"
    with admin.begin() as connection:
        original = connection.scalar(
            select(quality_items.c.state).where(quality_items.c.item_id == rig.item_id)
        )
        tampered = {**original, "context_revision": original["context_revision"] + 100}
        connection.execute(
            quality_items.update()
            .where(quality_items.c.item_id == rig.item_id)
            .values(state=tampered)
        )
    try:
        with pytest.raises(IntegrityViolation):
            rig.workflow.state(rig.item_id)
    finally:
        with admin.begin() as connection:
            connection.execute(
                quality_items.update()
                .where(quality_items.c.item_id == rig.item_id)
                .values(state=original)
            )
    for table in (
        "quality_bases",
        "quality_decisions",
        "quality_commands",
        "quality_context_versions",
        "quality_outbox",
        "quality_state_versions",
        "quality_external_streams",
    ):
        with pytest.raises(DBAPIError) as error:
            with admin.begin() as connection:
                connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        assert error.value.orig.sqlstate == "42501"


def test_postgres_outbox_chain_uses_previous_result_version_after_jsonb_reload(trace):
    _, writer, _, _, _, _ = trace
    rig = Rig(PostgresQualityWorkflow(writer), f"chain-{uuid4().hex}", checkpoints=("final",))
    for number in range(3):
        rig.observe(f"control-{number}")
    messages = sorted(rig.state()["outbox"], key=lambda value: value["result_version"])
    assert messages[0]["previous_message_id"] is None
    assert [value["previous_message_id"] for value in messages[1:]] == [
        value["message_id"] for value in messages[:-1]
    ]
