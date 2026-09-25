"""Generate deterministic, synthetic source events and separate expected outcomes."""
from __future__ import annotations

import argparse
import copy
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data"
STAMP = "2026-09-25T"


def event(event_id: str, event_type: str, minute: str, item: str | None, payload: dict, **links: object) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "1.0",
        "occurred_at": f"{STAMP}{minute}{'' if minute.count(':') == 2 else ':00'}Z",
        "source_id": "vision-emulator-1" if event_type == "inspection_observed" else "line-emulator-1",
        "item_id": item,
        "item_type_id": "bracket-demo",
        "line_id": "line-1",
        "station_id": links.pop("station_id", "incoming"),
        "operation_run_id": links.pop("operation_run_id", None),
        "operator_id": links.pop("operator_id", None),
        "equipment_id": links.pop("equipment_id", None),
        "identification_status": "reliable" if item else "uncertain",
        "time_quality": "synchronized",
        "payload": payload,
        **links,
    }


def inspect(event_id: str, minute: str, item: str, phase: str, result: str, defects: list[dict],
            *, station: str, run: str | None = None, quality: str = "good") -> dict:
    return event(event_id, "inspection_observed", minute, item, {
        "inspection_result": result, "checkpoint_id": f"{station}-{phase}",
        "inspection_phase": phase, "observation_quality": quality,
        "confidence": 0.91 if quality == "good" else None,
        "defects": defects, "evidence_refs": [], "analyzer_version": "emulator-1",
    }, station_id=station, operation_run_id=run)


def sign(ref: str, kind: str, component: str, region: str = "face-A") -> dict:
    return {"defect_ref": ref, "type": kind, "description": "Синтетический признак", "component_id": component, "region_id": region}


def operation(prefix: str, item: str, start: str, finish: str, station: str, operator: str,
              machine: str, kind: str, previous: str | None = None) -> list[dict]:
    run = f"run-{prefix}"
    payload = {"operation_type_id": kind, "shift_id": "shift-1"}
    if previous:
        payload["previous_operation_run_id"] = previous
    return [
        event(f"evt-{prefix}-start", "operation_started", start, item, payload,
              station_id=station, operation_run_id=run, operator_id=operator, equipment_id=machine),
        event(f"evt-{prefix}-finish", "operation_finished", finish, item, payload,
              station_id=station, operation_run_id=run, operator_id=operator, equipment_id=machine),
    ]


def make(seed: int) -> tuple[dict, dict]:
    rng = random.Random(seed)
    order = {
        "message_id": "erp-order-001-v1", "external_order_id": "order-001", "revision": 1,
        "item_type_id": "bracket-demo", "item_ids": ["assembly-001", "assembly-002", "assembly-003", "assembly-004"],
        "route_id": "route-bracket", "route_version": "1",
        "checkpoints": ["incoming-incoming", "station-machining-after_operation", "station-assembly-final"],
        "components": {
            "assembly-001": ["bracket-001", "insert-001"],
            "assembly-002": ["bracket-002", "insert-002"],
            "assembly-003": ["bracket-003", "insert-003"],
            "assembly-004": ["bracket-004", "insert-004"],
        },
    }
    received = lambda item, minute: event(f"evt-{item}-received", "item_received", minute, item, {
        "work_order_id": "order-001", "component_ids": order["components"][item], "assembly_version": "1"})
    first = [
        received("assembly-001", "10:01"),
        inspect("evt-001-incoming", "10:02", "assembly-001", "incoming", "no_defect_signs", [], station="incoming"),
        *operation("machining-001", "assembly-001", "10:03", "10:05", "station-machining", "operator-01", "machine-01", "machining"),
        inspect("evt-001-after-machining", "10:06", "assembly-001", "after_operation", "no_defect_signs", [], station="station-machining", run="run-machining-001"),
        operation("assembly-001", "assembly-001", "10:07", "10:09", "station-assembly", "operator-02", "machine-02", "assembly")[0],
        event("evt-machine-02-warning", "machine_state", "10:08", "assembly-001", {"state": "warning", "parameters": {"vibration": "above-demo-baseline"}, "description": "Синтетическое отклонение"}, station_id="station-assembly", operation_run_id="run-assembly-001", equipment_id="machine-02"),
        event("evt-001-operator-action", "operator_action", "10:08:30", "assembly-001", {"action_type": "adjust_fixture", "details": {"note": "Проверка фиксации после предупреждения станка"}}, station_id="station-assembly", operation_run_id="run-assembly-001", operator_id="operator-02", equipment_id="machine-02"),
        operation("assembly-001", "assembly-001", "10:07", "10:09", "station-assembly", "operator-02", "machine-02", "assembly")[1],
        inspect("evt-001-final", "10:10", "assembly-001", "final", "defect_signs", [sign("mark-001", "surface-mark", "insert-001")], station="station-assembly"),
        inspect("evt-001-final-repeat-observation", "10:11", "assembly-001", "final", "defect_signs", [sign("mark-001", "surface-mark", "insert-001")], station="station-assembly"),
    ]
    late = inspect("evt-001-late-incoming", "10:02", "assembly-001", "incoming", "defect_signs", [sign("mark-001", "surface-mark", "insert-001")], station="incoming")
    late["occurred_at"] = f"{STAMP}10:02:30Z"
    normal = [
        received("assembly-002", "11:01"),
        inspect("evt-002-incoming", "11:02", "assembly-002", "incoming", "no_defect_signs", [], station="incoming"),
        *operation("machining-002", "assembly-002", "11:03", "11:05", "station-machining", "operator-01", "machine-01", "machining"),
        inspect("evt-002-after", "11:06", "assembly-002", "after_operation", "no_defect_signs", [], station="station-machining", run="run-machining-002"),
        *operation("assembly-002", "assembly-002", "11:07", "11:09", "station-assembly", "operator-02", "machine-02", "assembly"),
        inspect("evt-002-final", "11:10", "assembly-002", "final", "no_defect_signs", [], station="station-assembly"),
    ]
    incoming = [
        received("assembly-003", "12:01"),
        inspect("evt-003-incoming", "12:02", "assembly-003", "incoming", "defect_signs", [sign("chip-003", "edge-chip", "bracket-003")], station="incoming"),
        *operation("machining-003", "assembly-003", "12:03", "12:05", "station-machining", "operator-01", "machine-01", "machining"),
        inspect("evt-003-after", "12:06", "assembly-003", "after_operation", "defect_signs", [sign("chip-003", "edge-chip", "bracket-003")], station="station-machining", run="run-machining-003"),
    ]
    uncertain = [
        received("assembly-004", "13:01"),
        inspect("evt-004-poor", "13:02", "assembly-004", "incoming", "no_defect_signs", [], station="incoming", quality="poor"),
        *operation("machining-004", "assembly-004", "13:03", "13:05", "station-machining", "operator-01", "machine-01", "machining"),
        inspect("evt-004-multi", "13:06", "assembly-004", "after_operation", "defect_signs", [
            sign("scratch-004", "scratch", "bracket-004"), sign("dent-004", "dent", "bracket-004", "face-B")], station="station-machining", run="run-machining-004"),
        *operation("machining-004-rework", "assembly-004", "13:07", "13:09", "station-machining", "operator-01", "machine-01", "machining", previous="run-machining-004"),
    ]
    # Stable seed changes only synthetic process parameters, never IDs or expected semantics.
    for events in (first, normal, incoming, uncertain):
        for source_event in events:
            if source_event["event_type"] == "machine_state":
                source_event["payload"]["parameters"]["severity_index"] = rng.randint(2, 8)
    unsupported = copy.deepcopy(first[0]); unsupported["event_id"] = "evt-unsupported-version"; unsupported["schema_version"] = "9.0"
    missing_id = copy.deepcopy(first[0]); missing_id.pop("event_id")
    invalid_enum = copy.deepcopy(first[-2]); invalid_enum["event_id"] = "evt-invalid-result"; invalid_enum["payload"]["inspection_result"] = "maybe"
    conflicting = copy.deepcopy(first[-2]); conflicting["payload"]["defects"][0]["description"] = "Другое содержимое с тем же ID"
    scenarios = {
        "metadata": {"synthetic": True, "seed": seed, "schema_version": "1.0"},
        "work_orders": [order],
        "negative_cases": [
            {"id": "unsupported-version", "event": unsupported},
            {"id": "missing-event-id", "event": missing_id},
            {"id": "invalid-enum", "event": invalid_enum},
            {"id": "conflicting-event-id", "event": conflicting},
        ],
        "scenarios": [
            {"id": "first-slice", "events": first, "duplicate_event_ids": ["evt-001-final"], "late_events": [late], "actions": [
                {"kind": "nonconformity", "item_id": "assembly-001", "decision": "confirm", "reason": "Признак подтверждён на двух наблюдениях; причина не установлена"},
                {"kind": "disposition", "item_id": "assembly-001", "disposition": "rework", "reason": "Требуется устранение подтверждённого дефекта"}]},
            {"id": "normal", "events": normal, "duplicate_event_ids": [], "late_events": [], "actions": [
                {"kind": "disposition", "item_id": "assembly-002", "disposition": "released", "reason": "Обязательные контрольные точки пройдены"}]},
            {"id": "incoming-defect", "events": incoming, "duplicate_event_ids": [], "late_events": [], "actions": [
                {"kind": "nonconformity", "item_id": "assembly-003", "decision": "request_check", "reason": "Входной признак требует дополнительной проверки"}]},
            {"id": "poor-and-multiple", "events": uncertain, "duplicate_event_ids": [], "late_events": [], "actions": [
                {"kind": "nonconformity", "item_id": "assembly-004", "decision": "request_check", "reason": "Неоценимый входной контроль и два признака требуют проверки"}]},
        ],
    }
    expected = {
        "metadata": {"synthetic": True, "seed": seed, "description": "Expected values are independent of input events."},
        "first-slice": {"before_decision": {"inspection_events": 4, "pending_cards": 1, "logical_defects": 1, "completed_operations": 2, "station_elapsed_seconds": [120, 120]}, "after_confirm_and_rework": {"inspected_items": 1, "confirmed_nonconforming_items": 1, "confirmed_defects": 1, "confirmed_operator_errors": 0, "rework_operations": 0, "disposition": "rework"}, "after_late_event": {"confirmed_defects": 1, "review_required": True, "cause_established": False}},
        "normal": {"inspected_items": 1, "confirmed_defects": 0, "completed_operations": 2, "station_elapsed_seconds": [120, 120]},
        "incoming-defect": {"pending_cards": 1, "cause_established": False, "confirmed_operator_errors": 0},
        "poor-and-multiple": {"effective_incoming_result": "not_assessable", "new_candidates": 2, "rework_operations": 1, "confirmed_operator_errors": 0},
        "negative_cases": {
            "unsupported-version": {"http_status": 422, "error_code": "unsupported_schema_version"},
            "missing-event-id": {"http_status": 422, "error_code": "validation_error"},
            "invalid-enum": {"http_status": 422, "error_code": "invalid_enum"},
            "conflicting-event-id": {"http_status": 409, "error_code": "conflict"},
        },
    }
    return scenarios, expected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    scenarios, expected = make(args.seed)
    OUT.mkdir(exist_ok=True)
    for name, value in (("scenarios.json", scenarios), ("expected.json", expected)):
        (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(scenarios['scenarios'])} synthetic scenarios with seed {args.seed}")


if __name__ == "__main__":
    main()
