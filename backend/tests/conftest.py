import json
import os

import pytest

from app.adapters.json_events import JsonEventCodec
from app.adapters.protection import AesGcmProtector


@pytest.fixture
def event_document():
    return {
        "event_id": "evt-001",
        "event_type": "inspection_observed",
        "schema_version": "1.0",
        "occurred_at": "2026-09-25T10:00:00Z",
        "source_id": "vision-01",
        "item_id": "part-001",
        "station_id": "incoming",
        "identification_status": "reliable",
        "time_quality": "synchronized",
        "payload": {
            "inspection_result": "no_defect_signs",
            "checkpoint_id": "incoming-surface",
            "inspection_phase": "incoming",
            "observation_quality": "good",
            "defects": [],
        },
    }


@pytest.fixture
def event_bytes(event_document):
    return json.dumps(event_document, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def codec():
    return JsonEventCodec()


@pytest.fixture
def protector():
    # Per-test random keys, never persisted or reused as deployment credentials.
    return AesGcmProtector({("test", 1): os.urandom(32)}, active_key=("test", 1))


@pytest.fixture
def trace_document():
    """Synthetic input builder; expected results are assertions in separate tests."""

    def make(event_id, kind="item_received", time="10:00:00", **changes):
        payloads = {
            "item_received": {
                "work_order_id": "order-001",
                "component_ids": ["bracket-001", "insert-001"],
                "assembly_version": "1",
            },
            "operation_started": {"operation_type_id": "machining", "shift_id": "shift-1"},
            "operation_finished": {"operation_type_id": "machining", "shift_id": "shift-1"},
            "inspection_observed": {
                "inspection_result": "no_defect_signs",
                "checkpoint_id": "incoming-surface",
                "inspection_phase": "incoming",
                "observation_quality": "good",
                "defects": [],
            },
            "operator_action": {"action_type": "mode-confirmed", "details": {}},
            "machine_state": {
                "state": "warning",
                "parameters": {"vibration_mm_s": 8.2},
                "description": "Synthetic threshold deviation",
            },
        }
        document = {
            "event_id": event_id,
            "event_type": kind,
            "schema_version": "1.0",
            "source_id": "source-01",
            "item_id": "assembly-001",
            "item_type_id": "bracket-demo",
            "occurred_at": f"2026-09-25T{time}Z",
            "identification_status": "reliable",
            "time_quality": "synchronized",
            "line_id": "line-1",
            "station_id": "machining",
            "operation_run_id": "run-001" if kind.startswith("operation_") else None,
            "operator_id": "operator-01",
            "equipment_id": "machine-01",
            "payload": payloads[kind],
        }
        document.update(changes)
        return document

    return make
