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
