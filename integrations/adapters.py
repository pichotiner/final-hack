"""Boundary transformations for illustrative ERP, MES and CAD messages.

These are file-contract adapters, not live vendor API connectors. They do not
decide whether a part is good or infer a cause.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class MappingError(ValueError):
    pass


def required_text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MappingError(f"{key} must be a nonempty string")
    return value


@dataclass
class IdMap:
    values: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def bind(self, system: str, entity_type: str, external_id: str, internal_id: str) -> None:
        key = (system, entity_type, external_id)
        existing = self.values.get(key)
        if existing is not None and existing != internal_id:
            raise MappingError(f"conflicting mapping: {key}")
        self.values[key] = internal_id

    def resolve(self, system: str, entity_type: str, external_id: str) -> str:
        try:
            return self.values[(system, entity_type, external_id)]
        except KeyError as exc:
            raise MappingError(f"unmapped {system}/{entity_type}/{external_id}") from exc


def from_1c_order(raw: dict, ids: IdMap) -> dict:
    """Map a sample 1C JSON export to the emulator's work-order format."""
    external_id = required_text(raw, "НомерЗаказа")
    item_id = required_text(raw, "СерийныйНомер")
    ids.bind("1c", "work_order", external_id, f"order-{external_id}")
    ids.bind("1c", "item", item_id, item_id)
    return {
        "message_id": required_text(raw, "ИдСообщения"),
        "external_order_id": external_id,
        "revision": int(raw["Ревизия"]),
        "item_type_id": required_text(raw, "Номенклатура"),
        "item_ids": [item_id],
        "route_id": required_text(raw, "Маршрут"),
        "route_version": required_text(raw, "ВерсияМаршрута"),
        "checkpoints": list(raw.get("КонтрольныеТочки", [])),
    }


def from_galaxy_order(raw: dict, ids: IdMap) -> dict:
    external_id = required_text(raw, "orderNo")
    item_id = required_text(raw, "batchItemNo")
    ids.bind("galaxy", "work_order", external_id, f"order-{external_id}")
    ids.bind("galaxy", "item", item_id, item_id)
    return {
        "message_id": required_text(raw, "messageId"),
        "external_order_id": external_id,
        "revision": int(raw["revision"]),
        "item_type_id": required_text(raw, "productCode"),
        "item_ids": [item_id],
        "route_id": required_text(raw, "routeCode"),
        "route_version": required_text(raw, "routeVersion"),
        "checkpoints": list(raw.get("controlPoints", [])),
    }


def from_mes_operation(raw: dict, ids: IdMap) -> dict:
    """Normalize MES completion to a source event; backend owns duration rules."""
    item_id = ids.resolve("mes", "item", required_text(raw, "unitId"))
    return {
        "event_id": required_text(raw, "messageId"),
        "event_type": "operation_finished",
        "schema_version": "1.0",
        "occurred_at": required_text(raw, "finishedAt"),
        "source_id": "mes-adapter-1",
        "item_id": item_id,
        "station_id": required_text(raw, "stationId"),
        "operation_run_id": required_text(raw, "runId"),
        "operator_id": raw.get("operatorId"),
        "equipment_id": raw.get("equipmentId"),
        "identification_status": "reliable",
        "time_quality": raw.get("timeQuality", "uncertain"),
        "payload": {"operation_type_id": required_text(raw, "operationCode"), "shift_id": required_text(raw, "shiftId")},
    }


def from_kompas_assembly(raw: dict) -> dict:
    """Import a conditional BOM export; geometry is deliberately absent."""
    assembly_id = required_text(raw, "assemblyId")
    version = required_text(raw, "version")
    components = raw.get("components")
    if not isinstance(components, list):
        raise MappingError("components must be a list")
    normalized = []
    for component in components:
        quantity = component.get("quantity")
        if type(quantity) is not int or quantity < 1:
            raise MappingError("quantity must be a positive integer")
        normalized.append({"id": required_text(component, "id"), "item_type_id": required_text(component, "partNo"), "quantity": quantity})
    return {"assembly_id": assembly_id, "assembly_version": version, "components": normalized, "geometry_available": False}


def to_erp_quality_result(message_id: str, external_order_id: str, external_item_id: str,
                          result_version: int, disposition: str, confirmed_ids: list[str],
                          cause_status: str, review_required: bool, previous_message_id: str | None = None) -> dict:
    if result_version < 1 or not message_id:
        raise MappingError("invalid result identity")
    return {
        "message_id": message_id, "external_order_id": external_order_id,
        "external_item_id": external_item_id, "result_version": result_version,
        "disposition": disposition, "confirmed_nonconformities": confirmed_ids,
        "cause_status": cause_status, "review_required": review_required,
        **({"previous_message_id": previous_message_id} if previous_message_id else {}),
    }
