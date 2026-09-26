"""The repository OpenAPI is the single shared transport schema, including ERP."""

import os
from functools import cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from app.domain.errors import IntegrityViolation, InvalidEvent


@cache
def document(external=False):
    root = Path(
        os.environ.get("QC_CONTRACT_ROOT", Path(__file__).resolve().parents[3] / "contracts")
    )
    name = "external-openapi.yaml" if external else "openapi.yaml"
    return yaml.safe_load((root / name).read_text(encoding="utf-8"))


@cache
def validator(name, external=False):
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": document(external)["components"]},
        format_checker=FormatChecker(),
    )


def validate(name, value, *, external=False, response=False):
    if next(validator(name, external).iter_errors(value), None) is not None:
        error = IntegrityViolation if response else InvalidEvent
        raise error("The document does not match the shared transport contract.")
