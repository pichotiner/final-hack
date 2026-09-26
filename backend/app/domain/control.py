"""Internal, versioned production context. These are not transport DTOs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlRequirement:
    checkpoint_id: str
    source_id: str
    analyzer_version: str
    component_id: str | None = None
    region_id: str | None = None
    operation_run_id: str | None = None
    inspection_phase: str = "final"
    control_session_id: str = ""

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (
                self.checkpoint_id,
                self.source_id,
                self.analyzer_version,
                self.control_session_id,
            )
        ):
            raise ValueError("A control requirement needs checkpoint, source and method version.")
        if self.inspection_phase not in {
            "incoming",
            "before_operation",
            "after_operation",
            "final",
        }:
            raise ValueError("A control requirement needs a supported inspection phase.")
        if self.inspection_phase in {"before_operation", "after_operation"} and not (
            self.operation_run_id
        ):
            raise ValueError("An operation control requirement needs a planned run.")


@dataclass(frozen=True)
class SourceAuthority:
    source_id: str
    event_types: tuple[str, ...]
    station_ids: tuple[str, ...] = ()

    def __post_init__(self):
        known = {
            "item_received",
            "operation_started",
            "operation_finished",
            "operator_action",
            "machine_state",
            "inspection_observed",
        }
        if (
            not self.source_id
            or not self.event_types
            or any(value not in known for value in self.event_types)
        ):
            raise ValueError("A source authority needs an identity and supported event types.")


@dataclass(frozen=True)
class ProductionContext:
    work_order_id: str
    work_order_version: str
    route_version: str
    assembly_version: str
    mapping_version: str
    external_system: str
    external_order_id: str
    external_item_id: str
    required_operations: tuple[str, ...]
    controls: tuple[ControlRequirement, ...]
    source_authorities: tuple[SourceAuthority, ...]
    component_ids: tuple[str, ...] = ()
    producer_id: str = "efqa-demonstrator"
    rules_version: str = "quality-v2"
    route_id: str | None = None
    control_plan_version: str | None = None

    def __post_init__(self):
        for name in (
            "work_order_id",
            "work_order_version",
            "route_version",
            "assembly_version",
            "mapping_version",
            "external_system",
            "external_order_id",
            "external_item_id",
            "producer_id",
            "rules_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must identify a pinned context version.")
        if not self.required_operations or len(set(self.required_operations)) != len(
            self.required_operations
        ):
            raise ValueError("The demonstrator requires distinct planned operation runs.")
        checkpoints = [control.checkpoint_id for control in self.controls]
        if not checkpoints or len(set(checkpoints)) != len(checkpoints):
            raise ValueError("Control checkpoints must be non-empty and distinct.")
        if any(
            control.operation_run_id is not None
            and control.operation_run_id not in self.required_operations
            for control in self.controls
        ):
            raise ValueError("A control must refer to a planned operation.")
        if not self.source_authorities:
            raise ValueError("The production context needs source authority rules.")
        allowed = {
            (authority.source_id, event_type)
            for authority in self.source_authorities
            for event_type in authority.event_types
        }
        if any(
            (control.source_id, "inspection_observed") not in allowed for control in self.controls
        ):
            raise ValueError("Every control source must be authorized for observations.")

    def authorizes(self, source_id: str, event_type: str, station_id: str | None) -> bool:
        return any(
            authority.source_id == source_id
            and event_type in authority.event_types
            and (not authority.station_ids or station_id in authority.station_ids)
            for authority in self.source_authorities
        )


def decode_context(value: dict | None) -> ProductionContext | None:
    if value is None:
        return None
    return ProductionContext(
        **{
            **value,
            "controls": tuple(ControlRequirement(**part) for part in value["controls"]),
            "source_authorities": tuple(
                SourceAuthority(
                    source_id=part["source_id"],
                    event_types=tuple(part["event_types"]),
                    station_ids=tuple(part.get("station_ids", ())),
                )
                for part in value["source_authorities"]
            ),
            "required_operations": tuple(value["required_operations"]),
            "component_ids": tuple(value["component_ids"]),
        }
    )
