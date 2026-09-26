"""Deterministic digital thread. No HTTP DTOs, database or inferred causation."""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from typing import Any
from uuid import UUID

from app.domain.errors import InvalidEnum, InvalidEvent
from app.domain.events import EventType, RecordMetadata

PROCESSOR_VERSION = "traceability-v1"


def _id(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidEvent(f"{name} must be a non-empty identifier of at most 128 characters.")
    return value


@dataclass(frozen=True)
class Duration:
    value: float | None
    kind: str
    origin: str
    unavailable_reason: str | None = None
    unit: str = "s"
    evidence_record_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class TraceFact:
    metadata: RecordMetadata
    item_type_id: str | None
    line_id: str | None
    station_id: str | None
    operation_run_id: str | None
    operator_id: str | None
    equipment_id: str | None
    work_order_id: str | None = None
    component_ids: tuple[str, ...] = ()
    assembly_version: str | None = None
    operation_type_id: str | None = None
    shift_id: str | None = None
    previous_operation_run_id: str | None = None
    reported_duration: Duration | None = None

    @property
    def event_type(self) -> EventType:
        return self.metadata.envelope.event_type


def trace_fact(metadata: RecordMetadata, document: dict) -> TraceFact:
    """Extract domain inputs; only receipt/operation payloads are supported here.

    This is deliberately not a replacement for the generated transport validator.
    Unsupported event payloads are retained by the journal and remain unresolved.
    """
    links = {
        name: _id(document.get(name), name)
        for name in (
            "item_type_id",
            "line_id",
            "station_id",
            "operation_run_id",
            "operator_id",
            "equipment_id",
        )
    }
    payload = document["payload"]
    kind = metadata.envelope.event_type
    fields: dict[str, Any] = {}
    if kind == EventType.ITEM_RECEIVED:
        _id(metadata.envelope.item_id, "item_id", required=True)
        _id(links["item_type_id"], "item_type_id", required=True)
        fields["work_order_id"] = _id(payload.get("work_order_id"), "work_order_id", required=True)
        components = payload.get("component_ids")
        if not isinstance(components, list):
            raise InvalidEvent("component_ids must be an array.")
        fields["component_ids"] = tuple(_id(c, "component_id", required=True) for c in components)
        if len(set(components)) != len(components) or metadata.envelope.item_id in components:
            raise InvalidEvent(
                "Component instance IDs must be distinct and cannot include the item."
            )
        fields["assembly_version"] = _id(
            payload.get("assembly_version"), "assembly_version", required=bool(components)
        )
    elif kind in (EventType.OPERATION_STARTED, EventType.OPERATION_FINISHED):
        _id(links["station_id"], "station_id", required=True)
        _id(links["operation_run_id"], "operation_run_id", required=True)
        for name in ("operation_type_id", "shift_id"):
            fields[name] = _id(payload.get(name), name, required=True)
        fields["previous_operation_run_id"] = _id(
            payload.get("previous_operation_run_id"), "previous_operation_run_id"
        )
        reported = payload.get("reported_duration")
        if reported is not None:
            if kind != EventType.OPERATION_FINISHED or not isinstance(reported, dict):
                raise InvalidEvent("reported_duration is supported on operation_finished only.")
            value = reported.get("value")
            if type(value) not in (int, float) or not isfinite(value) or value < 0:
                raise InvalidEvent("reported_duration.value must be a finite non-negative number.")
            if reported.get("unit") != "s" or reported.get("kind") not in (
                "active_processing",
                "station_elapsed",
            ):
                raise InvalidEnum("Unsupported reported duration unit or kind.")
            fields["reported_duration"] = Duration(
                value=float(value),
                kind=reported["kind"],
                origin="reported",
                evidence_record_ids=(metadata.record_id,),
            )
    return TraceFact(metadata=metadata, **links, **fields)


@dataclass(frozen=True)
class EventState:
    record_id: UUID
    status: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class TimelineEntry:
    record_id: UUID
    source_id: str
    event_id: str
    event_type: str
    occurred_at: datetime
    received_at: datetime
    time_quality: str
    line_id: str | None
    station_id: str | None
    operation_run_id: str | None
    operator_id: str | None
    equipment_id: str | None
    projection_status: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class Operation:
    operation_run_id: str
    operation_type_id: str | None
    station_id: str | None
    shift_id: str | None
    line_id: str | None
    operator_id: str | None
    equipment_id: str | None
    previous_operation_run_id: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    evidence_record_ids: tuple[UUID, ...]
    durations: tuple[Duration, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class ItemHistory:
    item_id: str
    item_type_id: str | None
    work_order_id: str | None
    component_ids: tuple[str, ...]
    assembly_version: str | None
    passport_record_ids: tuple[UUID, ...]
    issues: tuple[str, ...]
    operations: tuple[Operation, ...]
    timeline: tuple[TimelineEntry, ...]
    processor_version: str = PROCESSOR_VERSION


def _order(fact: TraceFact) -> tuple:
    event = fact.metadata.envelope
    return event.occurred_at, event.source_id, event.event_id


def _agreed(facts: list[TraceFact], field: str) -> Any:
    values = {getattr(fact, field) for fact in facts if getattr(fact, field) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _operation(
    run_id: str, facts: list[TraceFact], run_ids: set[str], item_type_id: str | None
) -> Operation:
    starts = [fact for fact in facts if fact.event_type == EventType.OPERATION_STARTED]
    finishes = [fact for fact in facts if fact.event_type == EventType.OPERATION_FINISHED]
    issues: list[str] = []
    fields = (
        "operation_type_id",
        "station_id",
        "shift_id",
        "line_id",
        "operator_id",
        "equipment_id",
        "previous_operation_run_id",
    )
    for name in fields:
        if len({getattr(f, name) for f in facts if getattr(f, name) is not None}) > 1:
            issues.append(f"conflicting_{name}")
    types = {f.item_type_id for f in facts if f.item_type_id is not None}
    if len(types) > 1 or (item_type_id is not None and any(t != item_type_id for t in types)):
        issues.append("conflicting_item_type_id")
    if len(starts) > 1 or len(finishes) > 1:
        issues.append("ambiguous_operation_boundary")
    previous = _agreed(facts, "previous_operation_run_id")
    if previous == run_id:
        issues.append("cyclic_rework_reference")
    invalid = bool(issues)
    if previous is not None and previous not in run_ids:
        issues.append("previous_operation_not_found")
    start = starts[0] if len(starts) == 1 else None
    finish = finishes[0] if len(finishes) == 1 else None
    started_at = start.metadata.envelope.occurred_at if start else None
    finished_at = finish.metadata.envelope.occurred_at if finish else None
    value = None
    reason = None
    if invalid:
        status, reason = "invalid", "conflicting_operation_evidence"
    elif start is None:
        status, reason = "incomplete", "missing_start"
        issues.append(reason)
    elif finish is None:
        status, reason = "in_progress", "missing_finish"
    elif finished_at < started_at:
        status, reason = "invalid", "negative_interval"
        issues.append(reason)
    else:
        status = "completed"
    if any(f.metadata.envelope.time_quality != "synchronized" for f in facts):
        issues.append("uncertain_source_time")
        reason = reason or "uncertain_source_time"
    if status == "completed" and reason is None:
        value = (finished_at - started_at).total_seconds()
    records = tuple(f.metadata.record_id for f in facts)
    durations = (
        Duration(value, "station_elapsed", "calculated", reason, evidence_record_ids=records),
    )
    durations += tuple(f.reported_duration for f in facts if f.reported_duration is not None)
    return Operation(
        operation_run_id=run_id,
        **{name: _agreed(facts, name) for name in fields},
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        evidence_record_ids=records,
        durations=durations,
        issues=tuple(sorted(set(issues))),
    )


def unlinked_state(fact: TraceFact) -> EventState:
    issues = ["unreliable_item_identification"]
    if fact.metadata.envelope.time_quality != "synchronized":
        issues.append("uncertain_source_time")
    return EventState(fact.metadata.record_id, "unresolved", tuple(issues))


def rebuild_history(item_id: str, source_facts: list[TraceFact]) -> ItemHistory:
    """Replay only reliable facts belonging to this item; input ordering is irrelevant."""
    facts = sorted(source_facts, key=_order)
    if not facts or any(
        f.metadata.envelope.item_id != item_id
        or f.metadata.envelope.identification_status != "reliable"
        for f in facts
    ):
        raise ValueError("Replay requires reliable facts for exactly one non-empty item history.")
    identities = [(f.metadata.envelope.source_id, f.metadata.envelope.event_id) for f in facts]
    if len(set(identities)) != len(identities):
        raise ValueError("Replay input must contain unique source event identities.")
    receipts = [f for f in facts if f.event_type == EventType.ITEM_RECEIVED]
    passport_fields = ("item_type_id", "work_order_id", "component_ids", "assembly_version")
    passport_values = {tuple(getattr(f, name) for name in passport_fields) for f in receipts}
    passport = receipts[0] if len(passport_values) == 1 else None
    item_issues = []
    if not receipts:
        item_issues.append("item_receipt_missing")
    elif passport is None:
        item_issues.append("conflicting_item_receipts")
    else:
        item_issues.append("work_order_not_resolved")
    groups: dict[str, list[TraceFact]] = defaultdict(list)
    for fact in facts:
        if fact.event_type in (EventType.OPERATION_STARTED, EventType.OPERATION_FINISHED):
            groups[fact.operation_run_id].append(fact)
    run_ids = set(groups)
    operations = {
        run_id: _operation(run_id, values, run_ids, passport.item_type_id if passport else None)
        for run_id, values in groups.items()
    }
    # A rework link is not temporal proof. Reject cycles without imposing an invented order.
    for run_id, operation in list(operations.items()):
        visited: set[str] = set()
        current = run_id
        while current in operations and current not in visited:
            visited.add(current)
            current = operations[current].previous_operation_run_id
        if current == run_id:
            calculated = replace(
                operation.durations[0], value=None, unavailable_reason="cyclic_rework_reference"
            )
            operations[run_id] = replace(
                operation,
                status="invalid",
                durations=(calculated, *operation.durations[1:]),
                issues=tuple(sorted(set((*operation.issues, "cyclic_rework_reference")))),
            )
    timeline = []
    for fact in facts:
        event = fact.metadata.envelope
        issues = []
        if passport is None:
            issues.extend(item_issues)
        elif fact.item_type_id is not None and fact.item_type_id != passport.item_type_id:
            issues.append("conflicting_item_type_id")
        if event.time_quality != "synchronized":
            issues.append("uncertain_source_time")
        if fact.event_type == EventType.ITEM_RECEIVED:
            issues.append("work_order_not_resolved")
        elif fact.event_type in (EventType.OPERATION_STARTED, EventType.OPERATION_FINISHED):
            issues.extend(operations[fact.operation_run_id].issues)
        else:
            issues.append("event_domain_processing_not_implemented")
        timeline.append(
            TimelineEntry(
                record_id=fact.metadata.record_id,
                source_id=event.source_id,
                event_id=event.event_id,
                event_type=event.event_type.value,
                occurred_at=event.occurred_at,
                received_at=fact.metadata.received_at,
                time_quality=event.time_quality,
                line_id=fact.line_id,
                station_id=fact.station_id,
                operation_run_id=fact.operation_run_id,
                operator_id=fact.operator_id,
                equipment_id=fact.equipment_id,
                projection_status="unresolved" if issues else "applied",
                issues=tuple(sorted(set(issues))),
            )
        )
    return ItemHistory(
        item_id=item_id,
        item_type_id=passport.item_type_id if passport else None,
        work_order_id=passport.work_order_id if passport else None,
        component_ids=passport.component_ids if passport else (),
        assembly_version=passport.assembly_version if passport else None,
        passport_record_ids=tuple(f.metadata.record_id for f in receipts),
        issues=tuple(item_issues),
        operations=tuple(operations.values()),
        timeline=tuple(timeline),
    )
