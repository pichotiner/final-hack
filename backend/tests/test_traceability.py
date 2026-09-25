import json
from datetime import UTC, datetime
from itertools import permutations
from uuid import uuid4

import pytest

from app.domain.errors import InvalidEnum, InvalidEvent
from app.domain.events import RecordMetadata
from app.domain.traceability import rebuild_history, trace_fact


@pytest.fixture
def fact(codec):
    def build(document):
        prepared = codec.decode(json.dumps(document).encode())
        metadata = RecordMetadata(
            uuid4(),
            prepared.envelope,
            datetime(2026, 9, 26, tzinfo=UTC),
            prepared.content_digest,
            "unresolved",
            (),
        )
        return trace_fact(metadata, document)

    return build


def test_delivery_order_does_not_change_history(trace_document, fact):
    receipt = fact(trace_document("received"))
    start = fact(trace_document("start", "operation_started", "10:03:00"))
    finish = fact(trace_document("finish", "operation_finished", "10:05:00"))
    expected = rebuild_history("assembly-001", [receipt, start, finish])
    for ordering in permutations([receipt, start, finish]):
        assert rebuild_history("assembly-001", list(ordering)) == expected
    operation = expected.operations[0]
    assert operation.status == "completed"
    assert operation.durations[0].value == 120
    assert operation.durations[0].origin == "calculated"
    assert expected.component_ids == ("bracket-001", "insert-001")
    assert expected.work_order_id == "order-001"
    assert expected.issues == ("work_order_not_resolved",)
    assert expected.timeline[-1].projection_status == "applied"


@pytest.mark.parametrize(
    ("kinds", "status", "reason"),
    [
        (["operation_started"], "in_progress", "missing_finish"),
        (["operation_finished"], "incomplete", "missing_start"),
    ],
)
def test_incomplete_boundaries_are_not_zero(trace_document, fact, kinds, status, reason):
    facts = [fact(trace_document("received"))]
    facts += [fact(trace_document(kind, kind)) for kind in kinds]
    op = rebuild_history("assembly-001", facts).operations[0]
    assert op.status == status
    assert op.durations[0].value is None
    assert op.durations[0].unavailable_reason == reason


@pytest.mark.parametrize(
    ("start_time", "finish_time", "quality", "status", "reason"),
    [
        ("10:05:00", "10:03:00", "synchronized", "invalid", "negative_interval"),
        ("10:03:00", "10:05:00", "uncertain", "completed", "uncertain_source_time"),
    ],
)
def test_unusable_clock_interval(
    trace_document, fact, start_time, finish_time, quality, status, reason
):
    facts = [
        fact(trace_document("received")),
        fact(trace_document("start", "operation_started", start_time, time_quality=quality)),
        fact(trace_document("finish", "operation_finished", finish_time)),
    ]
    history = rebuild_history("assembly-001", facts)
    assert history.operations[0].status == status
    assert history.operations[0].durations[0].value is None
    assert history.operations[0].durations[0].unavailable_reason == reason
    assert all(t.projection_status == "unresolved" for t in history.timeline)


def test_reported_active_time_is_distinct_from_elapsed(trace_document, fact):
    finish = trace_document("finish", "operation_finished", "10:05:00")
    finish["payload"]["reported_duration"] = {"value": 80, "unit": "s", "kind": "active_processing"}
    history = rebuild_history(
        "assembly-001",
        [
            fact(trace_document("received")),
            fact(trace_document("start", "operation_started", "10:03:00")),
            fact(finish),
        ],
    )
    calculated, reported = history.operations[0].durations
    assert (calculated.value, calculated.kind, calculated.origin) == (
        120,
        "station_elapsed",
        "calculated",
    )
    assert (reported.value, reported.kind, reported.origin) == (80, "active_processing", "reported")
    assert len(reported.evidence_record_ids) == 1


@pytest.mark.parametrize("field", ["station_id", "equipment_id", "operator_id", "item_type_id"])
def test_conflicting_boundaries_do_not_choose_a_winner(trace_document, fact, field):
    facts = [
        fact(trace_document("received")),
        fact(trace_document("start", "operation_started", "10:03:00")),
        fact(trace_document("finish", "operation_finished", "10:05:00", **{field: "other"})),
    ]
    op = rebuild_history("assembly-001", facts).operations[0]
    assert op.status == "invalid"
    assert f"conflicting_{field}" in op.issues
    assert op.durations[0].value is None


def test_multiple_distinct_start_events_are_ambiguous(trace_document, fact):
    facts = [
        fact(trace_document("received")),
        fact(trace_document("start-1", "operation_started", "10:03:00")),
        fact(trace_document("start-2", "operation_started", "10:03:00")),
        fact(trace_document("finish", "operation_finished", "10:05:00")),
    ]
    op = rebuild_history("assembly-001", facts).operations[0]
    assert op.status == "invalid"
    assert op.started_at is None
    assert "ambiguous_operation_boundary" in op.issues


def test_missing_or_conflicting_passport_is_explicit(trace_document, fact):
    start = fact(trace_document("start", "operation_started"))
    missing = rebuild_history("assembly-001", [start])
    assert missing.item_type_id is None
    assert missing.issues == ("item_receipt_missing",)
    first = fact(trace_document("receipt-1"))
    other_doc = trace_document("receipt-2")
    other_doc["payload"]["component_ids"] = ["different-component"]
    conflicting = rebuild_history("assembly-001", [first, fact(other_doc), start])
    assert conflicting.issues == ("conflicting_item_receipts",)
    assert conflicting.component_ids == ()
    assert len(conflicting.passport_record_ids) == 2
    assert all(t.projection_status == "unresolved" for t in conflicting.timeline)


def test_unknown_optional_links_are_not_invented(trace_document, fact):
    facts = [
        fact(trace_document("received")),
        fact(
            trace_document(
                "start", "operation_started", operator_id=None, equipment_id=None, line_id=None
            )
        ),
    ]
    op = rebuild_history("assembly-001", facts).operations[0]
    assert op.operator_id is None and op.equipment_id is None and op.line_id is None


def test_matching_boundaries_with_wrong_passport_type_are_invalid(trace_document, fact):
    facts = [fact(trace_document("received"))]
    facts += [
        fact(trace_document(kind, kind, time, item_type_id="wrong-type"))
        for kind, time in [("operation_started", "10:03:00"), ("operation_finished", "10:05:00")]
    ]
    op = rebuild_history("assembly-001", facts).operations[0]
    assert op.status == "invalid"
    assert op.durations[0].value is None
    assert "conflicting_item_type_id" in op.issues


def test_equal_boundary_timestamps_are_zero_interval_not_active_time(trace_document, fact):
    facts = [
        fact(trace_document("received")),
        fact(trace_document("start", "operation_started", "10:03:00")),
        fact(trace_document("finish", "operation_finished", "10:03:00")),
    ]
    op = rebuild_history("assembly-001", facts).operations[0]
    assert op.status == "completed"
    assert op.durations[0].value == 0
    assert op.durations[0].kind == "station_elapsed"
    assert len(op.durations) == 1


def test_duplicate_facts_cannot_be_counted_by_reducer(trace_document, fact):
    received = fact(trace_document("received"))
    with pytest.raises(ValueError, match="unique source event"):
        rebuild_history("assembly-001", [received, received])


def test_rework_missing_reference_and_cycle(trace_document, fact):
    first = trace_document("start", "operation_started")
    second = trace_document("rework", "operation_started", operation_run_id="run-002")
    second["payload"]["previous_operation_run_id"] = "run-001"
    missing = rebuild_history("assembly-001", [fact(second)])
    assert missing.operations[0].issues == ("previous_operation_not_found",)
    valid = rebuild_history("assembly-001", [fact(first), fact(second)])
    assert len(valid.operations) == 2
    rework = next(op for op in valid.operations if op.operation_run_id == "run-002")
    assert rework.previous_operation_run_id == "run-001"
    assert not rework.issues
    first["payload"]["previous_operation_run_id"] = "run-002"
    cyclic = rebuild_history("assembly-001", [fact(first), fact(second)])
    assert all(o.status == "invalid" for o in cyclic.operations)
    assert all("cyclic_rework_reference" in o.issues for o in cyclic.operations)


@pytest.mark.parametrize(
    "change",
    [
        {"item_id": "other-item"},
        {"identification_status": "uncertain"},
    ],
)
def test_replay_cannot_attach_uncertain_or_other_item(trace_document, fact, change):
    with pytest.raises(ValueError, match="reliable facts"):
        rebuild_history("assembly-001", [fact(trace_document("received", **change))])


def test_tie_breaker_uses_source_and_event_ids(trace_document, fact):
    facts = [
        fact(trace_document("z", "inspection_observed", source_id="b")),
        fact(trace_document("b", "inspection_observed", source_id="a")),
        fact(trace_document("a", "inspection_observed", source_id="a")),
    ]
    history = rebuild_history("assembly-001", facts)
    assert [(t.source_id, t.event_id) for t in history.timeline] == [
        ("a", "a"),
        ("a", "b"),
        ("b", "z"),
    ]
    assert all("event_domain_processing_not_implemented" in t.issues for t in history.timeline)


@pytest.mark.parametrize("field", ["operation_run_id", "station_id"])
def test_required_operation_link_rejected(trace_document, fact, field):
    with pytest.raises(InvalidEvent):
        fact(trace_document("start", "operation_started", **{field: None}))


@pytest.mark.parametrize("components", [None, "component", ["x", "x"], ["assembly-001"], [""]])
def test_invalid_component_identity_rejected(trace_document, fact, components):
    document = trace_document("received")
    document["payload"]["component_ids"] = components
    with pytest.raises(InvalidEvent):
        fact(document)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"value": -1, "unit": "s", "kind": "active_processing"}, InvalidEvent),
        ({"value": True, "unit": "s", "kind": "active_processing"}, InvalidEvent),
        ({"value": "12", "unit": "s", "kind": "active_processing"}, InvalidEvent),
        ({"value": 12, "unit": "ms", "kind": "active_processing"}, InvalidEnum),
    ],
)
def test_invalid_reported_duration_rejected(trace_document, fact, value, error):
    document = trace_document("finish", "operation_finished")
    document["payload"]["reported_duration"] = value
    with pytest.raises(error):
        fact(document)
