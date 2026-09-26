import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from openapi_spec_validator import validate as validate_openapi
from pydantic import TypeAdapter, ValidationError as DtoError

from contracts.generated.external import Ack, QualityResult
from contracts.generated.models import ResolveCommand, SourceEvent
from contracts.validation import document, strict_json, validate, validate_source_event

ROOT = Path(__file__).resolve().parents[1]


def test_specs_and_versions():
    for external in (False, True):
        doc = document(external)
        validate_openapi(doc)
        assert doc["info"]["version"] == "0.3.0"
        assert all("/v2/" in path for path in doc["paths"])


def test_all_fixture_events_schema_generated_and_semantic_validation():
    feed = json.loads((ROOT / "data/v2/source-events.json").read_text())["events"]
    for event in feed.values():
        validate_source_event(event)
        TypeAdapter(SourceEvent).validate_json(json.dumps(event), strict=True)
    for order in json.loads((ROOT / "data/v2/work-orders.json").read_text()):
        validate("WorkOrder", order, external=True)
    for result in json.loads((ROOT / "data/v2/erp-results.json").read_text()):
        validate("QualityResult", result, external=True)
        QualityResult.model_validate(result, strict=True)


@pytest.mark.parametrize(
    "edit",
    [
        lambda x: x.pop("station_id"),
        lambda x: x.update(schema_version="2.0"),
        lambda x: x["payload"].update(inspection_result="OK"),
        lambda x: x["payload"].update(confidence=1.1),
        lambda x: x.update(occurred_at="2026-09-25T10:00:00"),
    ],
)
def test_bad_source_structure_is_rejected_by_schema_and_dto(edit):
    event = json.loads((ROOT / "data/v2/source-events.json").read_text())["events"][
        "final-1"
    ]
    edit(event)
    with pytest.raises((ValueError, ValidationError)):
        validate_source_event(event)
    # Pydantic parses date-time but permits naive times: RFC3339 timezone remains boundary validation.
    if event["occurred_at"].endswith("Z"):
        with pytest.raises(DtoError):
            TypeAdapter(SourceEvent).validate_json(json.dumps(event), strict=True)


@pytest.mark.parametrize(
    "edit",
    [
        lambda x: x.update(item_id=None),
        lambda x: x["payload"].update(defects=[]),
        lambda x: x["payload"].update(inspection_result="no_defect_signs"),
        lambda x: (
            x["payload"].update(inspection_phase="after_operation"),
            x.update(operation_run_id=None),
        ),
    ],
)
def test_cross_field_rules_are_enforced_explicitly_not_claimed_generated(edit):
    event = json.loads((ROOT / "data/v2/source-events.json").read_text())["events"][
        "final-1"
    ]
    edit(event)
    with pytest.raises(ValueError):
        validate_source_event(event)


def test_unknown_optional_fields_and_null_are_preserved_at_boundary():
    event = json.loads((ROOT / "data/v2/source-events.json").read_text())["events"][
        "incoming-1"
    ]
    event["future_sensor_field"] = {"opaque": 17}
    raw = json.dumps(event).encode()
    decoded = strict_json(raw)
    validate_source_event(decoded)
    assert decoded == event and decoded["operator_id"] is None
    with pytest.raises(ValueError):
        strict_json(b'{"event_id":"a","event_id":"b"}')
    with pytest.raises(ValueError):
        strict_json(b'{"confidence":NaN}')


def test_author_is_not_a_command_input_and_resolve_reference_is_control_record():
    value = {
        "action": "resolve",
        "reason": "repeat control",
        "expected_revision": 12,
        "expected_basis_id": "b-12",
        "idempotency_key": "resolve-1",
        "rework_operation_run_id": "run-rework-001",
        "previous_operation_run_id": "run-assembly-001",
        "control_record_ids": ["record-good-control"],
    }
    validate("ResolveCommand", value)
    ResolveCommand.model_validate(value, strict=True)
    with pytest.raises(ValidationError):
        validate("ResolveCommand", value | {"actor_id": "forged"})
    with pytest.raises(ValidationError):
        validate("ResolveCommand", value | {"control_record_ids": []})


def test_ack_requires_stream_digest_and_initial_commit_metadata():
    ack = {
        "producer_id": "efqa-demo",
        "external_system": "erp-demo",
        "external_order_id": "order-001",
        "external_item_id": "assembly-001",
        "message_id": "m1",
        "result_version": 1,
        "content_digest": "a" * 64,
        "status": "accepted",
        "application_status": "applied",
        "received_at": "2026-09-26T10:00:00Z",
        "current_version_at_accept": 1,
    }
    validate("Ack", ack, external=True)
    Ack.model_validate_json(json.dumps(ack), strict=True)
    for field in ("content_digest", "external_order_id", "current_version_at_accept"):
        partial = copy.deepcopy(ack)
        del partial[field]
        with pytest.raises(ValidationError):
            validate("Ack", partial, external=True)


def test_acceptance_oracles_cover_every_dp01_case_and_do_not_come_from_generator():
    plan = json.loads((ROOT / "data/v2/acceptance.json").read_text())
    assert {case["id"] for case in plan["cases"]} >= {f"A{i:02d}" for i in range(1, 15)}
    assert (
        next(x for x in plan["cases"] if x["id"] == "A04")["expected"][
            "confirmed_defects"
        ]
        == 1
    )
