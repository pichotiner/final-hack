"""Inbound ERP boundary: supplied OpenAPI plus explicit, pinned production mapping."""

import copy
import json
from pathlib import Path

import httpx
import rfc8785
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from app.adapters.json_events import _invalid_constant, _object
from app.adapters.tls import require_https, trust
from app.domain.control import decode_context
from app.domain.errors import InvalidEvent


def strict_document(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant)
        rfc8785.dumps(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidEvent("Invalid integration JSON.") from error


class ErpContract:
    """Consume the editor's specification; never maintain a second ERP DTO schema."""

    def __init__(self, path):
        try:
            document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            schemas = document["components"]["schemas"]
            if document["openapi"] != "3.1.0":
                raise ValueError
            for name in ("WorkOrder", "WorkOrderPage"):
                if name not in schemas:
                    raise ValueError

            def local_refs(value):
                if isinstance(value, dict):
                    if "$ref" in value and not value["$ref"].startswith("#/"):
                        raise ValueError
                    for part in value.values():
                        local_refs(part)
                elif isinstance(value, list):
                    for part in value:
                        local_refs(part)

            local_refs(document)
            self.validators = {
                name: Draft202012Validator(
                    {"$ref": f"#/components/schemas/{name}", "components": document["components"]},
                    format_checker=FormatChecker(),
                )
                for name in ("WorkOrder", "WorkOrderPage")
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as error:
            raise InvalidEvent("Cannot load the external ERP OpenAPI contract.") from error

    def validate(self, name, value):
        if next(self.validators[name].iter_errors(value), None) is not None:
            raise InvalidEvent("ERP document does not match the supplied contract.")


class WorkOrderMapper:
    def __init__(self, profile, contract):
        self.profile = copy.deepcopy(profile)
        self.contract = contract
        try:
            for name in ("profile_id", "profile_version", "external_system", "producer_id"):
                value = profile[name]
                if not isinstance(value, str) or not value.strip() or len(value) > 128:
                    raise ValueError
            if not isinstance(profile["items"], dict) or not profile["items"]:
                raise ValueError
            rfc8785.dumps(profile)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidEvent("Invalid ERP mapping profile.") from error

    def map(self, order):
        self.contract.validate("WorkOrder", order)
        profile = self.profile
        try:
            expected = profile["expected_order"]
            for name in (
                "external_order_id",
                "item_type_id",
                "route_id",
                "route_version",
                "control_plan_version",
            ):
                if expected[name] != order[name]:
                    raise ValueError
            if set(order["item_ids"]) != set(profile["items"]) or set(order["components"]) != set(
                profile["items"]
            ):
                raise ValueError
            checkpoints = {part["checkpoint_id"]: part for part in order["checkpoints"]}
            if len(checkpoints) != len(order["checkpoints"]) or any(
                part["repeat_after_rework"] is not True for part in checkpoints.values()
            ):
                raise ValueError
            contexts = {}
            for external_id in order["item_ids"]:
                mapping = profile["items"][external_id]
                item_id = mapping["item_id"]
                if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128:
                    raise ValueError
                if item_id in contexts:
                    raise ValueError
                components = order["components"][external_id]
                if len(components) != len(set(components)) or set(components) != set(
                    mapping["component_mapping"]
                ):
                    raise ValueError
                mapped_components = [mapping["component_mapping"][key] for key in components]
                if len(mapped_components) != len(set(mapped_components)):
                    raise ValueError
                value = {
                    **mapping["context"],
                    "work_order_version": str(order["revision"]),
                    "route_id": order["route_id"],
                    "route_version": order["route_version"],
                    "control_plan_version": order["control_plan_version"],
                    "mapping_version": f"{profile['profile_id']}@{profile['profile_version']}",
                    "producer_id": profile["producer_id"],
                    "external_system": profile["external_system"],
                    "external_order_id": order["external_order_id"],
                    "external_item_id": external_id,
                    "component_ids": mapped_components,
                }
                # Reuse the existing canonical context schema, not a second external schema.
                canonical = json.loads(
                    Path(__file__)
                    .parents[1]
                    .joinpath("api/contract.json")
                    .read_text(encoding="utf-8")
                )
                validator = Draft202012Validator(
                    {
                        "$ref": "#/components/schemas/ProductionContext",
                        "components": canonical["components"],
                    }
                )
                if next(validator.iter_errors(value), None) is not None:
                    raise ValueError
                context = decode_context(value)
                if {rule.checkpoint_id for rule in context.controls} != set(checkpoints):
                    raise ValueError
                for rule in context.controls:
                    point = checkpoints[rule.checkpoint_id]
                    if (rule.region_id, rule.analyzer_version) != (
                        point["region_id"],
                        point["method_version"],
                    ) or (
                        rule.component_id is not None and rule.component_id not in mapped_components
                    ):
                        raise ValueError
                    if rule.inspection_phase == "incoming" and rule.operation_run_id is not None:
                        raise ValueError
                contexts[item_id] = context
            return contexts
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise InvalidEvent(
                "ERP order is not covered by the pinned production profile."
            ) from error


class HttpWorkOrders:
    def __init__(self, url, token, contract, *, client=None, verify=True):
        require_https(url)
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.userinfo:
            raise ValueError("An HTTP ERP endpoint without embedded credentials is required.")
        if parsed.query or parsed.fragment or not token.strip():
            raise ValueError("An ERP endpoint and bearer credential are required.")
        self.url, self.token, self.contract = url, token, contract
        self.client = client or httpx.Client(
            timeout=15, follow_redirects=False, trust_env=False, verify=trust(verify)
        )
        self.owns_client = client is None

    def orders(self):
        cursor, seen = None, set()
        for _ in range(1000):
            try:
                with self.client.stream(
                    "GET",
                    self.url,
                    params={} if cursor is None else {"cursor": cursor},
                    headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                    follow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise InvalidEvent(
                            f"ERP work-order fetch failed: HTTP {response.status_code}."
                        )
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1_048_576:
                            raise InvalidEvent("ERP work-order page exceeds the size limit.")
                page = strict_document(bytes(raw))
                self.contract.validate("WorkOrderPage", page)
                # Validate the entire page before importing any order from it.
                for order in page["items"]:
                    self.contract.validate("WorkOrder", order)
                next_cursor = page["next_cursor"]
                if next_cursor is not None:
                    if (
                        not isinstance(next_cursor, str)
                        or not next_cursor.isascii()
                        or not next_cursor.isdecimal()
                    ):
                        raise InvalidEvent("Invalid ERP pagination cursor.")
                    next_cursor = next_cursor.lstrip("0") or "0"
                    if next_cursor in seen or next_cursor == "0":
                        raise InvalidEvent("ERP pagination did not advance.")
                    seen.add(next_cursor)
                yield from page["items"]
                if next_cursor is None:
                    return
                cursor = next_cursor
            except httpx.TransportError as error:
                raise InvalidEvent(
                    "ERP work-order transport unavailable; retry the import."
                ) from error
        raise InvalidEvent("ERP pagination exceeds the page limit.")

    def close(self):
        if self.owns_client:
            self.client.close()
