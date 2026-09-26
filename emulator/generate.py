"""Generate fixed synthetic source inputs only. Expected results are handwritten separately."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inputs() -> dict:
    events = {}

    def event(
        key,
        kind,
        minute,
        *,
        item="assembly-001",
        run=None,
        station="station-assembly",
        source="line-emulator-1",
        payload=None,
        **fields,
    ):
        value = {
            "event_id": key,
            "event_type": kind,
            "schema_version": "1.0",
            "occurred_at": f"2026-09-25T10:{minute}:00Z",
            "source_id": source,
            "item_id": item,
            "item_type_id": "bracket-demo",
            "line_id": "line-1",
            "station_id": station,
            "operation_run_id": run,
            "operator_id": None,
            "equipment_id": None,
            "identification_status": "reliable",
            "time_quality": "synchronized",
            "payload": payload or {},
            **fields,
        }
        events[key] = value

    def inspect(
        key,
        minute,
        checkpoint,
        phase,
        result="no_defect_signs",
        quality="good",
        *,
        item="assembly-001",
        cycle="cycle-1",
        run=None,
        **fields,
    ):
        suffix = item.rsplit("-", 1)[-1]
        payload = {
            "checkpoint_id": checkpoint,
            "inspection_phase": phase,
            "inspection_result": result,
            "observation_quality": quality,
            "confidence": None,
            "analyzer_version": "emulator-1",
            "evidence_refs": [],
            "control_cycle_id": cycle,
            "defects": [
                {
                    "defect_ref": "mark-001",
                    "type": "surface-mark",
                    "component_id": f"insert-{suffix}",
                    "region_id": "face-A",
                }
            ]
            if result == "defect_signs"
            else [],
        }
        event(
            key,
            "inspection_observed",
            minute,
            source="vision-emulator-1",
            payload=payload,
            item=item,
            run=run,
            **fields,
        )

    event(
        "received-1",
        "item_received",
        "01",
        payload={
            "work_order_id": "order-001",
            "component_ids": ["bracket-001", "insert-001"],
            "assembly_version": "1",
        },
    )
    inspect("incoming-1", "02", "incoming-surface", "incoming")
    for kind, minute in [("operation_started", "03"), ("operation_finished", "05")]:
        event(
            f"machining-{kind}",
            kind,
            minute,
            run="run-machining-001",
            station="station-machining",
            operator_id="operator-01",
            equipment_id="machine-01",
            payload={"operation_type_id": "machining", "shift_id": "shift-1"},
        )
    inspect(
        "after-machining-1",
        "06",
        "after-machining",
        "after_operation",
        run="run-machining-001",
        station="station-machining",
    )
    event(
        "assembly-start",
        "operation_started",
        "07",
        run="run-assembly-001",
        operator_id="operator-02",
        equipment_id="machine-02",
        payload={"operation_type_id": "assembly", "shift_id": "shift-1"},
    )
    event(
        "warning-1",
        "machine_state",
        "08",
        run="run-assembly-001",
        equipment_id="machine-02",
        payload={
            "state": "warning",
            "parameters": {"load_percent": 115},
            "description": "Synthetic deviation, not an established cause",
        },
    )
    event(
        "action-1",
        "operator_action",
        "08",
        run="run-assembly-001",
        operator_id="operator-02",
        payload={"action_type": "program_acknowledged", "details": {}},
    )
    event(
        "assembly-end",
        "operation_finished",
        "09",
        run="run-assembly-001",
        payload={"operation_type_id": "assembly", "shift_id": "shift-1"},
    )
    inspect(
        "final-1",
        "10",
        "final-surface",
        "final",
        "defect_signs",
        run="run-assembly-001",
    )
    inspect(
        "final-2",
        "11",
        "final-surface",
        "final",
        "defect_signs",
        run="run-assembly-001",
    )
    base_ids = list(events)
    inspect("late-incoming", "02", "incoming-surface", "incoming", "defect_signs")
    events["late-incoming"]["occurred_at"] = "2026-09-25T10:02:30Z"
    for kind, minute in [("operation_started", "30"), ("operation_finished", "32")]:
        event(
            f"rework-{kind}",
            kind,
            minute,
            run="run-rework-001",
            payload={
                "operation_type_id": "rework",
                "shift_id": "shift-1",
                "previous_operation_run_id": "run-assembly-001",
                "control_cycle_id": "cycle-2",
            },
        )
    for key, checkpoint, phase, minute in [
        ("incoming-recheck", "incoming-surface", "incoming", "33"),
        ("machining-recheck", "after-machining", "after_operation", "34"),
        ("final-recheck", "final-surface", "final", "35"),
    ]:
        inspect(key, minute, checkpoint, phase, cycle="cycle-2", run="run-rework-001")
    inspect(
        "poor-final",
        "10",
        "final-surface",
        "final",
        quality="poor",
        item="assembly-002",
    )
    inspect(
        "unknown-time",
        "10",
        "final-surface",
        "final",
        "defect_signs",
        time_quality="uncertain",
    )
    # Null identity is explicit; the region still belongs to a candidate, not a proven assembly.
    events["unknown-item"] = deepcopy(events["final-1"])
    events["unknown-item"].update(
        event_id="unknown-item", item_id=None, identification_status="uncertain"
    )
    inspect(
        "normal-incoming", "02", "incoming-surface", "incoming", item="assembly-003"
    )
    inspect(
        "normal-machining",
        "06",
        "after-machining",
        "after_operation",
        item="assembly-003",
        run="run-machining-003",
    )
    inspect("normal-final", "10", "final-surface", "final", item="assembly-003")
    return {
        "synthetic": True,
        "assumptions": [
            "One synthetic source preserves mark-001",
            "Fixture times synchronized unless explicit uncertain",
            "Assignments and installations supplied by separate authorized composition fixture",
        ],
        "events": events,
        "base_order": base_ids,
        "late_order": ["late-incoming"],
        "rework_order": [
            "rework-operation_started",
            "rework-operation_finished",
            "incoming-recheck",
            "machining-recheck",
            "final-recheck",
        ],
    }


def main():
    destination = ROOT / "data/v2/source-events.json"
    destination.write_text(
        json.dumps(inputs(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Generated {len(inputs()['events'])} synthetic source inputs; expected files untouched"
    )


if __name__ == "__main__":
    main()
