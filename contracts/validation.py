"""Structural validation plus source-envelope rules not expressed by codegen.

Unknown optional fields in source events remain in the original accepted JSON.
Never round-trip a raw event through DTO serialization before storage/dedupe.
Stateful basis/permissions/completeness checks belong to role 1's core.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent


@lru_cache
def document(external: bool = False) -> dict:
    name = "external-openapi.yaml" if external else "openapi.yaml"
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


@lru_cache
def validator(name: str, external: bool = False) -> Draft202012Validator:
    doc = document(external)
    uri = "urn:efqa:external" if external else "urn:efqa:core"
    resource = Resource.from_contents(doc, default_specification=DRAFT202012)
    registry = Registry().with_resource(uri, resource)
    return Draft202012Validator(
        {"$ref": f"{uri}#/components/schemas/{name}"},
        registry=registry,
        format_checker=FormatChecker(),
    )


def validate(name: str, value: object, *, external: bool = False) -> None:
    validator(name, external).validate(value)


def strict_json(raw: bytes) -> object:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def bad_number(_):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_number)


def validate_source_event(event: dict) -> None:
    validate("SourceEvent", event)
    if (
        event.get("identification_status", "uncertain") == "reliable"
        and event["item_id"] is None
    ):
        raise ValueError("Reliable identification requires item_id")
    kind, payload = event["event_type"], event["payload"]
    if kind == "inspection_observed":
        if payload["inspection_phase"] in {"before_operation", "after_operation"}:
            if not event.get("operation_run_id"):
                raise ValueError("before/after inspection requires operation_run_id")
        result, defects = payload["inspection_result"], payload["defects"]
        if result == "defect_signs" and not defects:
            raise ValueError("defect_signs requires a nonempty defects array")
        if result == "no_defect_signs" and defects:
            raise ValueError("no_defect_signs requires an empty defects array")
    if (
        kind == "item_received"
        and payload["component_ids"]
        and not payload.get("assembly_version")
    ):
        raise ValueError("Assembly arrival requires assembly_version")
