"""Semantic source validation shared by the future generated transport boundary."""

from app.domain.errors import InvalidEnum, InvalidEvent


def validate_source_payload(document: dict) -> None:
    kind = document["event_type"]
    payload = document["payload"]

    def identifier(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise InvalidEvent("A required source identifier is missing or invalid.")

    if kind != "item_received":
        identifier(document.get("station_id"))
    if kind == "machine_state":
        identifier(document.get("equipment_id"))
        if payload.get("state") not in {"normal", "warning", "stopped"}:
            raise InvalidEnum("Unsupported machine state.")
        if not isinstance(payload.get("parameters", {}), dict):
            raise InvalidEvent("Machine parameters must be an object.")
    if kind == "operator_action":
        identifier(payload.get("action_type"))
        if not isinstance(payload.get("details", {}), dict):
            raise InvalidEvent("Action details must be an object.")
    if kind != "inspection_observed":
        return
    identifier(payload.get("checkpoint_id"))
    identifier(payload.get("control_session_id"))
    identifier(payload.get("analyzer_version"))
    for name, choices in {
        "inspection_result": {"defect_signs", "no_defect_signs", "not_assessable"},
        "observation_quality": {"good", "poor", "unknown"},
        "inspection_phase": {"incoming", "before_operation", "after_operation", "final"},
    }.items():
        if name not in payload:
            raise InvalidEvent("Required inspection field missing.")
        if not isinstance(payload[name], str) or payload[name] not in choices:
            raise InvalidEnum("Unsupported inspection enumeration.")
    if payload["inspection_phase"] in {"before_operation", "after_operation"}:
        identifier(document.get("operation_run_id"))
    defects = payload.get("defects")
    if not isinstance(defects, list):
        raise InvalidEvent("Inspection defects must be an array.")
    if (payload["inspection_result"] == "defect_signs" and not defects) or (
        payload["inspection_result"] == "no_defect_signs" and defects
    ):
        raise InvalidEvent("Inspection result and defect list disagree.")
    for defect in defects:
        if not isinstance(defect, dict):
            raise InvalidEvent("Each defect must be an object.")
        identifier(defect.get("type"))
        for key in ("defect_ref", "component_id", "region_id"):
            if defect.get(key) is not None:
                identifier(defect[key])
    confidence = payload.get("confidence")
    if confidence is not None and (
        type(confidence) not in (int, float) or not 0 <= confidence <= 1
    ):
        raise InvalidEvent("Confidence must be a number between zero and one, or null.")
    refs = payload.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise InvalidEvent("Evidence references must be an array of strings.")
