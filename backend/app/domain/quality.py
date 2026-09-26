"""The first executable quality-decision slice.

This module deliberately contains the domain transaction before a transport or
database adapter.  ``QualityWorkflow`` is an executable reference aggregate for
the DP-01 demonstrator: it pins immutable bases, rejects stale commands, keeps
defect existence separate from resolution, and emits complete ERP snapshots.
An adapter can persist the same command boundaries under the PostgreSQL guard.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

import rfc8785

from app.domain.control import ProductionContext
from app.domain.errors import (
    AckMismatch,
    BasisMismatch,
    IdempotencyConflict,
    InvalidTransition,
    PermissionDenied,
    ReleaseBlocked,
    StaleRevision,
    UnknownEntity,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(value: object) -> str:
    def canonical(value):
        if isinstance(value, (list, tuple)):
            return [canonical(part) for part in value]
        if isinstance(value, dict):
            return {key: canonical(part) for key, part in value.items()}
        return value

    return hashlib.sha256(rfc8785.dumps(canonical(value))).hexdigest()


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


@dataclass(frozen=True)
class Observation:
    observation_id: str
    item_id: str
    cycle_id: str | None
    checkpoint_id: str
    result: str
    effective_result: str
    quality: str
    defect_ref: str | None
    defect_type: str | None
    source_event_id: str | None
    created_at: datetime
    replaces_observation_id: str | None = None
    source_id: str | None = None
    component_id: str | None = None
    region_id: str | None = None
    analyzer_version: str | None = None
    operation_run_id: str | None = None
    record_id: str | None = None
    occurred_at: datetime | None = None
    time_quality: str = "uncertain"
    identification_status: str = "uncertain"
    inspection_phase: str | None = None
    control_session_id: str | None = None
    defect_index: int | None = None


@dataclass(frozen=True)
class ReworkOperation:
    operation_id: str
    item_id: str
    cycle_id: str
    previous_operation_run_id: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None
    time_quality: str = "uncertain"


@dataclass(frozen=True)
class Basis:
    basis_id: str
    item_id: str
    target_id: str | None
    context_revision: int
    cycle_id: str
    observation_ids: tuple[str, ...]
    nonconformity_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    created_at: datetime
    context_snapshot: dict = field(default_factory=dict)
    evidence_record_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    analysis_snapshot: dict = field(default_factory=dict)
    processor_version: str = "quality-v3"
    control_assessments: tuple[dict, ...] = ()


@dataclass
class Nonconformity:
    nonconformity_id: str
    item_id: str
    candidate_key: tuple[str | None, ...]
    status: str = "pending_review"
    resolution_status: str = "not_applicable"
    review_required: bool = False
    decision_ids: list[str] = field(default_factory=list)
    resolution_ids: list[str] = field(default_factory=list)
    cause_status: str = "unresolved"
    cause_category: str = "unknown"
    cause_evidence_record_ids: tuple[str, ...] = ()
    cause_limitations: tuple[str, ...] = ("cause_not_established",)


@dataclass(frozen=True)
class Decision:
    decision_id: str
    command_type: str
    target_id: str
    action: str
    reason: str
    actor_id: str
    basis_id: str
    reviewed_revision: int
    context_revision: int
    supersedes_id: str | None
    created_at: datetime
    rework_operation_id: str | None = None
    observation_ids: tuple[str, ...] = ()
    cause_status: str = "unresolved"
    cause_category: str = "unknown"
    cause_evidence_record_ids: tuple[str, ...] = ()
    operation_run_id: str | None = None
    operator_id: str | None = None
    action_record_id: str | None = None
    control_record_ids: tuple[str, ...] = ()
    control_binding: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    item_id: str
    result_version: int
    previous_message_id: str | None
    context_revision: int
    payload: dict
    content_digest: str
    status: str = "pending"
    ack: dict | None = None
    attempts: int = 0
    created_at: datetime = field(default_factory=_now)


@dataclass
class _Item:
    item_id: str
    cycle_id: str
    required_checkpoints: tuple[str, ...]
    route_known: bool = False
    context_revision: int = 0
    observations: dict[str, Observation] = field(default_factory=dict)
    rework_operations: dict[str, ReworkOperation] = field(default_factory=dict)
    nonconformities: dict[str, Nonconformity] = field(default_factory=dict)
    bases: dict[str, Basis] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    dispositions: list[Decision] = field(default_factory=list)
    outbox: dict[str, OutboxMessage] = field(default_factory=dict)
    next_result_version: int = 1
    release_allowed_now: bool = False
    blockers: tuple[str, ...] = ()
    review_required: bool = False
    disposition_review_required: bool = False
    initial_cycle_id: str = "cycle-1"
    context: ProductionContext | None = None
    production_history: dict = field(default_factory=dict)
    cycles: dict[str, dict] = field(default_factory=dict)
    evidence_record_ids: list[str] = field(default_factory=list)
    scope_constraints: dict[str, str] = field(default_factory=dict)
    observation_bindings: dict[str, dict] = field(default_factory=dict)
    control_replacements: dict[str, str] = field(default_factory=dict)


class QualityWorkflow:
    """Atomic reference implementation of the role №1 P0 command semantics."""

    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self.clock = clock
        self._items: dict[str, _Item] = {}
        self._commands: dict[tuple[str, str, str, str], tuple[str, dict]] = {}
        self._lock = RLock()
        self._batch_depth = 0

    def open_item(
        self,
        item_id: str,
        *,
        required_checkpoints: Iterable[str] = ("incoming", "final"),
        cycle_id: str = "cycle-1",
        route_known: bool = False,
        context: ProductionContext | None = None,
    ) -> None:
        item_id = _id(item_id, "item_id")
        if context is not None:
            required_checkpoints = tuple(rule.checkpoint_id for rule in context.controls)
            route_known = True
        checkpoints = tuple(
            dict.fromkeys(_id(value, "checkpoint_id") for value in required_checkpoints)
        )
        if not checkpoints:
            raise ValueError("At least one required checkpoint is needed.")
        with self._lock:
            if item_id in self._items:
                raise ValueError("The item is already open.")
            self._items[item_id] = _Item(
                item_id,
                _id(cycle_id, "cycle_id"),
                checkpoints,
                route_known=route_known,
                initial_cycle_id=cycle_id,
                context=context,
                cycles={
                    cycle_id: {
                        "disposition_id": None,
                        "operation_run_ids": list(context.required_operations) if context else [],
                        "required_checkpoints": checkpoints,
                        "control_sessions": (
                            {
                                rule.checkpoint_id: rule.control_session_id
                                for rule in context.controls
                            }
                            if context
                            else {}
                        ),
                        "checkpoint_operation_run_ids": (
                            {rule.checkpoint_id: rule.operation_run_id for rule in context.controls}
                            if context
                            else {}
                        ),
                    }
                },
            )

    def start_cycle(
        self,
        item_id: str,
        *,
        cycle_id: str,
        required_checkpoints: Iterable[str] | None = None,
        actor_id: str,
        expected_revision: int,
        expected_basis_id: str,
        idempotency_key: str,
        reason: str,
        operation_run_ids: Iterable[str] = (),
        control_sessions: dict[str, str] | None = None,
        checkpoint_operation_run_ids: dict[str, str | None] | None = None,
        rework_disposition_id: str | None = None,
    ) -> dict:
        """Open the explicitly assigned cycle after a rework disposition."""
        item = self._item(item_id)
        payload = {
            "actor_id": actor_id,
            "cycle_id": cycle_id,
            "required_checkpoints": list(
                item.required_checkpoints if required_checkpoints is None else required_checkpoints
            ),
            "operation_run_ids": list(operation_run_ids),
            "control_sessions": dict(control_sessions or {}),
            "checkpoint_operation_run_ids": dict(checkpoint_operation_run_ids or {}),
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
            "reason": reason,
            "rework_disposition_id": rework_disposition_id,
        }

        def apply() -> dict:
            self._check_context(item, expected_revision, expected_basis_id)
            if not item.dispositions or item.dispositions[-1].action != "rework":
                raise InvalidTransition("A new cycle requires the latest disposition to be rework.")
            if cycle_id in item.cycles:
                raise InvalidTransition("A previously used control cycle cannot be reopened.")
            disposition_id = item.dispositions[-1].decision_id
            if rework_disposition_id is not None and disposition_id != rework_disposition_id:
                raise InvalidTransition("The cycle must reference the current rework disposition.")
            if any(cycle["disposition_id"] == disposition_id for cycle in item.cycles.values()):
                raise InvalidTransition("This rework assignment already has a control cycle.")
            if set(payload["required_checkpoints"]) != set(item.required_checkpoints):
                raise InvalidTransition("Changing the control plan requires a versioned import.")
            runs = payload["operation_run_ids"]
            used = {run for cycle in item.cycles.values() for run in cycle["operation_run_ids"]}
            if item.context:
                used.update(item.context.required_operations)
            if not runs or len(set(runs)) != len(runs) or used.intersection(runs):
                raise InvalidTransition("A new cycle needs distinct, newly planned operation runs.")
            for run in runs:
                _id(run, "operation_run_id")
            sessions = payload["control_sessions"]
            run_by_checkpoint = payload["checkpoint_operation_run_ids"]
            checkpoints = set(item.required_checkpoints)
            if set(sessions) != checkpoints or set(run_by_checkpoint) != checkpoints:
                raise InvalidTransition(
                    "Every checkpoint in a new cycle needs a registered control session."
                )
            previous_sessions = {
                session
                for cycle in item.cycles.values()
                for session in cycle.get("control_sessions", {}).values()
            }
            if (
                any(not isinstance(value, str) or not value for value in sessions.values())
                or len(set(sessions.values())) != len(sessions)
                or previous_sessions.intersection(sessions.values())
                or any(
                    value is not None and value not in runs for value in run_by_checkpoint.values()
                )
            ):
                raise InvalidTransition(
                    "Control sessions and operation bindings must be new and planned."
                )
            if item.context and any(
                (
                    rule.inspection_phase == "incoming"
                    and run_by_checkpoint[rule.checkpoint_id] is not None
                )
                or (
                    rule.inspection_phase != "incoming"
                    and run_by_checkpoint[rule.checkpoint_id] is None
                )
                for rule in item.context.controls
            ):
                raise InvalidTransition("Control phase and operation binding do not agree.")
            item.cycle_id = _id(cycle_id, "cycle_id")
            item.required_checkpoints = tuple(payload["required_checkpoints"])
            item.cycles[cycle_id] = {
                "disposition_id": disposition_id,
                "operation_run_ids": runs,
                "required_checkpoints": item.required_checkpoints,
                "control_sessions": sessions,
                "checkpoint_operation_run_ids": run_by_checkpoint,
            }
            item.context_revision += 1
            result = {
                "cycle_id": item.cycle_id,
                "context_revision": item.context_revision,
                "control_sessions": sessions,
                "checkpoint_operation_run_ids": run_by_checkpoint,
            }
            self._emit(item)
            return result

        return self._command(item_id, "control_cycle", item_id, idempotency_key, payload, apply)

    def record_observation(
        self,
        item_id: str,
        *,
        observation_id: str,
        checkpoint_id: str,
        result: str,
        quality: str = "good",
        defect_ref: str | None = None,
        defect_type: str | None = None,
        source_event_id: str | None = None,
        cycle_id: str | None = None,
        replaces_observation_id: str | None = None,
        source_id: str | None = None,
        component_id: str | None = None,
        region_id: str | None = None,
        analyzer_version: str | None = None,
        operation_run_id: str | None = None,
        record_id: str | None = None,
        occurred_at: datetime | None = None,
        time_quality: str = "uncertain",
        identification_status: str = "uncertain",
        inspection_phase: str | None = None,
        control_session_id: str | None = None,
        defect_index: int | None = None,
    ) -> dict:
        """Attach one trusted observation and create/update its defect candidate."""
        item = self._item(item_id)
        observation_id = _id(observation_id, "observation_id")
        checkpoint_id = _id(checkpoint_id, "checkpoint_id")
        if result not in {"defect_signs", "no_defect_signs", "not_assessable"}:
            raise ValueError("Unsupported inspection result.")
        if quality not in {"good", "poor", "unknown"}:
            raise ValueError("Unsupported observation quality.")
        if result == "defect_signs" and not defect_type:
            raise ValueError("defect_signs requires defect_type.")
        if occurred_at is not None and (
            occurred_at.tzinfo is None or occurred_at.utcoffset() is None
        ):
            raise ValueError("Observation time must have a timezone.")
        if cycle_id is not None and cycle_id not in item.cycles:
            raise InvalidTransition("The observation refers to an unregistered cycle.")
        if quality in {"poor", "unknown"}:
            effective = "not_assessable"
        else:
            effective = result
        with self._lock:
            if observation_id in item.observations:
                saved = item.observations[observation_id]
                if (
                    saved.checkpoint_id != checkpoint_id
                    or saved.cycle_id != cycle_id
                    or saved.result != result
                    or saved.quality != quality
                    or saved.defect_ref != defect_ref
                    or saved.defect_type != defect_type
                    or saved.source_event_id != source_event_id
                    or saved.replaces_observation_id != replaces_observation_id
                    or any(
                        getattr(saved, name) != value
                        for name, value in {
                            "source_id": source_id,
                            "component_id": component_id,
                            "region_id": region_id,
                            "analyzer_version": analyzer_version,
                            "operation_run_id": operation_run_id,
                            "record_id": record_id,
                            "occurred_at": occurred_at,
                            "time_quality": time_quality,
                            "identification_status": identification_status,
                            "inspection_phase": inspection_phase,
                            "control_session_id": control_session_id,
                            "defect_index": defect_index,
                        }.items()
                    )
                ):
                    raise IdempotencyConflict("Observation ID was reused with different content.")
                return {
                    "status": "duplicate",
                    "observation_id": saved.observation_id,
                    "context_revision": item.context_revision,
                }
            observation = Observation(
                observation_id=observation_id,
                item_id=item_id,
                cycle_id=cycle_id,
                checkpoint_id=checkpoint_id,
                result=result,
                effective_result=effective,
                quality=quality,
                defect_ref=defect_ref,
                defect_type=defect_type,
                source_event_id=source_event_id,
                created_at=self.clock(),
                replaces_observation_id=replaces_observation_id,
                source_id=source_id,
                component_id=component_id,
                region_id=region_id,
                analyzer_version=analyzer_version,
                operation_run_id=operation_run_id,
                record_id=record_id,
                occurred_at=occurred_at,
                time_quality=time_quality,
                identification_status=identification_status,
                inspection_phase=inspection_phase,
                control_session_id=control_session_id,
                defect_index=defect_index,
            )
            if replaces_observation_id is not None:
                replaced = item.observations.get(replaces_observation_id)
                if replaced is None or not self._valid_repeat(item, replaced, observation):
                    raise InvalidTransition(
                        "Replacement must be a later, assessable comparable control."
                    )
            item.observations[observation_id] = observation
            if item.dispositions and item.dispositions[-1].action == "released":
                item.disposition_review_required = True
            if defect_type is not None:
                candidate_key = self._candidate_key(observation)
                existing = next(
                    (
                        nc
                        for nc in item.nonconformities.values()
                        if nc.candidate_key == candidate_key
                    ),
                    None,
                )
                if existing is None:
                    nc = Nonconformity(str(uuid4()), item_id, candidate_key)
                    item.nonconformities[nc.nonconformity_id] = nc
                elif existing.decision_ids:
                    existing.review_required = True
                    item.review_required = True
            item.context_revision += 1
            self._emit(item)
            return {
                "status": "accepted",
                "observation_id": observation_id,
                "effective_result": effective,
                "context_revision": item.context_revision,
            }

    @staticmethod
    def _effective_observation(item, observation):
        binding = item.observation_bindings.get(observation.observation_id)
        return (
            replace(
                observation,
                **{
                    key: binding[key]
                    for key in ("cycle_id", "control_session_id", "operation_run_id")
                },
            )
            if binding
            else observation
        )

    @classmethod
    def _effective_observations(cls, item):
        return [cls._effective_observation(item, o) for o in item.observations.values()]

    @classmethod
    def _valid_repeat(cls, item, old, new):
        old = cls._effective_observation(item, old)
        new = cls._effective_observation(item, new)
        fields = (
            "cycle_id",
            "checkpoint_id",
            "component_id",
            "region_id",
            "source_id",
            "analyzer_version",
            "operation_run_id",
            "inspection_phase",
            "control_session_id",
        )
        return (
            old.effective_result == "not_assessable"
            and old.cycle_id is not None
            and old.observation_id not in item.control_replacements
            and not any(
                o.replaces_observation_id == old.observation_id for o in item.observations.values()
            )
            and old.occurred_at is not None
            and new.occurred_at is not None
            and new.occurred_at > old.occurred_at
            and cls._usable(item, new)
            and all(getattr(old, key) == getattr(new, key) for key in fields)
        )

    def recover_control(
        self,
        item_id,
        *,
        action,
        record_id,
        actor_id,
        reason,
        expected_revision,
        expected_basis_id,
        idempotency_key,
        replacement_record_id=None,
        control_cycle_id=None,
    ):
        """Append a reviewed interpretation; never rewrite source observations."""
        item = self._item(item_id)
        payload = dict(
            action=action,
            record_id=record_id,
            actor_id=actor_id,
            reason=reason,
            expected_revision=expected_revision,
            expected_basis_id=expected_basis_id,
            replacement_record_id=replacement_record_id,
            control_cycle_id=control_cycle_id,
        )

        def apply():
            self._check_context(item, expected_revision, expected_basis_id)
            observations = [
                o for o in self._effective_observations(item) if o.record_id == record_id
            ]
            if (
                not observations
                or record_id not in item.bases[expected_basis_id].evidence_record_ids
            ):
                raise InvalidTransition(
                    "Control record must belong to the reviewed item and basis."
                )
            binding = {}
            decision_id = str(uuid4())
            records = [record_id]
            if action == "bind_control":
                if replacement_record_id is not None or control_cycle_id not in item.cycles:
                    raise InvalidTransition("Select a registered cycle, without a replacement.")
                plan = item.cycles[control_cycle_id]
                for observation in observations:
                    if (
                        observation.cycle_id is not None
                        or observation.checkpoint_id not in plan["control_sessions"]
                    ):
                        raise InvalidTransition("Only an unresolved control can receive a binding.")
                    binding = {
                        "cycle_id": control_cycle_id,
                        "control_session_id": plan["control_sessions"][observation.checkpoint_id],
                        "operation_run_id": plan["checkpoint_operation_run_ids"][
                            observation.checkpoint_id
                        ],
                    }
                    item.observation_bindings[observation.observation_id] = {
                        **binding,
                        "decision_id": decision_id,
                    }
            elif action == "replace_control":
                if control_cycle_id is not None or replacement_record_id == record_id:
                    raise InvalidTransition("Replacement requires a different control record.")
                candidates = [
                    o
                    for o in self._effective_observations(item)
                    if o.record_id == replacement_record_id
                ]
                if (
                    not candidates
                    or replacement_record_id
                    not in item.bases[expected_basis_id].evidence_record_ids
                ):
                    raise InvalidTransition("Replacement must belong to the reviewed basis.")
                if any(not self._usable(item, o) for o in candidates):
                    raise InvalidTransition("Replacement control must be assessable.")
                for old in observations:
                    matches = [o for o in candidates if self._valid_repeat(item, old, o)]
                    if not matches:
                        raise InvalidTransition(
                            "Repeat must be later, comparable and replace a poor control."
                        )
                    item.control_replacements[old.observation_id] = matches[0].observation_id
                records.append(replacement_record_id)
            else:
                raise InvalidTransition("Unsupported control recovery action.")
            item.context_revision += 1
            decision = Decision(
                decision_id,
                "control_recovery",
                item_id,
                action,
                reason,
                actor_id,
                expected_basis_id,
                expected_revision,
                item.context_revision,
                None,
                self.clock(),
                observation_ids=tuple(o.observation_id for o in observations),
                control_record_ids=tuple(records),
                control_binding=binding,
            )
            item.decisions.append(decision)
            for nc in item.nonconformities.values():
                if nc.decision_ids and any(
                    self._candidate_key(o) == nc.candidate_key for o in observations
                ):
                    nc.review_required = True
            item.review_required = any(nc.review_required for nc in item.nonconformities.values())
            if item.dispositions:
                item.disposition_review_required = True
            self._emit(item)
            return self._decision_view(decision)

        return self._command(item_id, "control_recovery", item_id, idempotency_key, payload, apply)

    def circumstances(self, item):
        from app.domain.analysis import circumstances

        return circumstances(
            item,
            self._effective_observations(item),
            lambda o: self._usable(item, o),
            self._candidate_key,
        )

    def create_basis(self, item_id: str, target_id: str | None = None) -> dict:
        item = self._item(item_id)
        with self._lock:
            if target_id is not None and target_id not in item.nonconformities:
                raise UnknownEntity("Unknown nonconformity in this item.")
            basis = Basis(
                basis_id=str(uuid4()),
                item_id=item_id,
                target_id=target_id,
                context_revision=item.context_revision,
                cycle_id=item.cycle_id,
                observation_ids=tuple(item.observations),
                nonconformity_ids=tuple(item.nonconformities),
                blockers=self._blockers(item),
                created_at=self.clock(),
                context_snapshot={
                    "production_context": asdict(item.context) if item.context else None,
                    "production_history": copy.deepcopy(item.production_history),
                    "cycles": copy.deepcopy(item.cycles),
                },
                evidence_record_ids=tuple(item.evidence_record_ids),
                decision_ids=tuple(d.decision_id for d in [*item.decisions, *item.dispositions]),
                analysis_snapshot={
                    "nonconformities": [self._nc_view(n) for n in item.nonconformities.values()],
                    "observations": [self._observation_view(o) for o in item.observations.values()],
                    "scope_constraints": copy.deepcopy(item.scope_constraints),
                    "observation_bindings": copy.deepcopy(item.observation_bindings),
                    "control_replacements": copy.deepcopy(item.control_replacements),
                    "circumstances": self.circumstances(item),
                    "kpi": self._kpi(item),
                },
                control_assessments=self._control_assessments(item),
            )
            item.bases[basis.basis_id] = basis
            return self._basis_view(basis)

    def record_rework_operation(
        self,
        item_id: str,
        *,
        operation_id: str,
        previous_operation_run_id: str,
        status: str = "completed",
        cycle_id: str | None = None,
        finished_at: datetime | None = None,
        time_quality: str = "uncertain",
    ) -> dict:
        """Record the actual rework operation; disposition alone is not execution."""
        item = self._item(item_id)
        operation_id = _id(operation_id, "operation_id")
        previous_operation_run_id = _id(previous_operation_run_id, "previous_operation_run_id")
        if status not in {"in_progress", "completed", "invalid"}:
            raise ValueError("Unsupported rework operation status.")
        with self._lock:
            saved = item.rework_operations.get(operation_id)
            if saved is not None:
                same = not (
                    saved.previous_operation_run_id != previous_operation_run_id
                    or saved.status != status
                    or saved.cycle_id != cycle_id
                    or saved.finished_at != finished_at
                    or saved.time_quality != time_quality
                )
                if same:
                    return {
                        "status": "duplicate",
                        "operation_id": operation_id,
                        "context_revision": item.context_revision,
                    }
                if not (
                    saved.status == "in_progress"
                    and status in {"completed", "invalid"}
                    and saved.cycle_id == cycle_id
                    and saved.previous_operation_run_id == previous_operation_run_id
                ):
                    raise InvalidTransition(
                        "A rework operation ID cannot be reused with different facts."
                    )
            if (
                item.cycle_id == item.initial_cycle_id
                or not item.dispositions
                or item.dispositions[-1].action != "rework"
            ):
                raise InvalidTransition("A rework operation requires an assigned rework cycle.")
            if cycle_id != item.cycle_id:
                raise InvalidTransition("A rework operation must belong to the active cycle.")
            if operation_id not in item.cycles[item.cycle_id]["operation_run_ids"]:
                raise InvalidTransition("The operation was not planned in this cycle.")
            known_prior = set(item.context.required_operations) if item.context else set()
            known_prior.update(item.rework_operations)
            if (
                previous_operation_run_id not in known_prior
                or previous_operation_run_id == operation_id
            ):
                raise InvalidTransition("The previous operation must exist on this item.")
            if finished_at is not None and (
                finished_at.tzinfo is None or finished_at.utcoffset() is None
            ):
                raise ValueError("Operation time must have a timezone.")
            operation = ReworkOperation(
                operation_id=operation_id,
                item_id=item_id,
                cycle_id=cycle_id or item.cycle_id,
                previous_operation_run_id=previous_operation_run_id,
                status=status,
                created_at=self.clock(),
                finished_at=finished_at,
                time_quality=time_quality,
            )
            item.rework_operations[operation_id] = operation
            item.context_revision += 1
            self._emit(item)
            return {
                "status": "accepted",
                "operation_id": operation_id,
                "context_revision": item.context_revision,
            }

    def decide_nonconformity(
        self,
        nonconformity_id: str,
        *,
        action: str,
        reason: str,
        actor_id: str,
        expected_revision: int,
        expected_basis_id: str,
        idempotency_key: str,
        cause_status: str = "unresolved",
        cause_category: str = "unknown",
        cause_evidence_record_ids: Iterable[str] = (),
        operation_run_id: str | None = None,
        operator_id: str | None = None,
        action_record_id: str | None = None,
    ) -> dict:
        item, nc = self._nonconformity(nonconformity_id)
        cause_evidence_record_ids = tuple(cause_evidence_record_ids)
        payload = {
            "actor_id": actor_id,
            "action": action,
            "reason": reason,
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
            "cause_status": cause_status,
            "cause_category": cause_category,
            "cause_evidence_record_ids": cause_evidence_record_ids,
            "operation_run_id": operation_run_id,
            "operator_id": operator_id,
            "action_record_id": action_record_id,
        }

        def apply() -> dict:
            self._check_context(
                item, expected_revision, expected_basis_id, target_id=nonconformity_id
            )
            if action not in {"confirm", "reject", "request_check"} or not reason.strip():
                raise InvalidTransition(
                    "A decision requires a supported action and non-empty reason."
                )
            if action == "confirm" and nc.status not in {"pending_review", "needs_check"}:
                if nc.status not in {"confirmed", "rejected"} or not nc.review_required:
                    raise InvalidTransition(
                        "Only a pending or review-required defect can be confirmed."
                    )
            if action == "reject" and nc.status == "rejected" and not nc.review_required:
                raise InvalidTransition("The nonconformity is already rejected.")
            self._validate_cause(
                item,
                action=action,
                basis_id=expected_basis_id,
                cause_status=cause_status,
                cause_category=cause_category,
                evidence_record_ids=cause_evidence_record_ids,
                operation_run_id=operation_run_id,
                operator_id=operator_id,
                action_record_id=action_record_id,
            )
            old = nc.decision_ids[-1] if nc.decision_ids else None
            item.context_revision += 1
            was_resolved = nc.resolution_status == "resolved"
            nc.status = {
                "confirm": "confirmed",
                "reject": "rejected",
                "request_check": "needs_check",
            }[action]
            nc.resolution_status = (
                "resolved"
                if action == "confirm" and was_resolved
                else "open"
                if action == "confirm"
                else "not_applicable"
            )
            if action != "confirm" or not was_resolved:
                nc.review_required = False
            nc.cause_status = cause_status if action == "confirm" else "unresolved"
            nc.cause_category = cause_category if action == "confirm" else "unknown"
            nc.cause_evidence_record_ids = cause_evidence_record_ids if action == "confirm" else ()
            nc.cause_limitations = (
                () if nc.cause_status == "established" else ("cause_not_established",)
            )
            item.review_required = any(
                value.review_required for value in item.nonconformities.values()
            )
            decision = Decision(
                decision_id=str(uuid4()),
                command_type="nonconformity_decision",
                target_id=nonconformity_id,
                action=action,
                reason=reason,
                actor_id=actor_id,
                basis_id=expected_basis_id,
                reviewed_revision=expected_revision,
                context_revision=item.context_revision,
                supersedes_id=old,
                created_at=self.clock(),
                cause_status=nc.cause_status,
                cause_category=nc.cause_category,
                cause_evidence_record_ids=nc.cause_evidence_record_ids,
                operation_run_id=operation_run_id,
                operator_id=operator_id,
                action_record_id=action_record_id,
            )
            item.decisions.append(decision)
            nc.decision_ids.append(decision.decision_id)
            self._emit(item)
            return self._decision_view(decision)

        return self._command(
            item.item_id,
            "nonconformity_decision",
            nonconformity_id,
            idempotency_key,
            payload,
            apply,
        )

    def resolve_nonconformity(
        self,
        nonconformity_id: str,
        *,
        reason: str,
        actor_id: str,
        expected_revision: int,
        expected_basis_id: str,
        idempotency_key: str,
        rework_operation_id: str,
        previous_operation_run_id: str,
        observation_ids: Iterable[str],
        control_record_ids: Iterable[str] = (),
    ) -> dict:
        item, nc = self._nonconformity(nonconformity_id)
        observation_ids = tuple(observation_ids)
        control_record_ids = tuple(control_record_ids)
        payload = {
            "actor_id": actor_id,
            "action": "resolve",
            "reason": reason,
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
            "rework_operation_id": rework_operation_id,
            "previous_operation_run_id": previous_operation_run_id,
            "observation_ids": observation_ids,
            "control_record_ids": control_record_ids,
        }

        def apply() -> dict:
            self._check_context(
                item, expected_revision, expected_basis_id, target_id=nonconformity_id
            )
            if nc.status != "confirmed":
                raise InvalidTransition("Only a confirmed nonconformity can be resolved.")
            if not rework_operation_id or not previous_operation_run_id:
                raise InvalidTransition(
                    "Resolution requires a completed rework linked to the prior run."
                )
            operation = item.rework_operations.get(rework_operation_id)
            if (
                operation is None
                or operation.status != "completed"
                or operation.previous_operation_run_id != previous_operation_run_id
                or operation.cycle_id != item.cycle_id
            ):
                raise InvalidTransition(
                    "Resolution requires the registered completed rework operation for this cycle."
                )
            observations = [item.observations.get(value) for value in observation_ids]
            basis = item.bases[expected_basis_id]
            if not set(observation_ids).issubset(basis.observation_ids) or not set(
                control_record_ids
            ).issubset(basis.evidence_record_ids):
                raise InvalidTransition("Repeat control must belong to the reviewed basis.")
            if not observations or any(value is None for value in observations):
                raise InvalidTransition("Resolution requires existing repeat-control observations.")
            observations = [self._effective_observation(item, value) for value in observations]
            if any(
                value.effective_result != "no_defect_signs" or value.quality != "good"
                for value in observations
                if value
            ):
                raise InvalidTransition("Resolution requires a good repeat control.")
            defect_checkpoints = {
                value.checkpoint_id
                for value in item.observations.values()
                if self._candidate_key(value) == nc.candidate_key
            }
            if any(
                value.cycle_id != item.cycle_id
                or value.checkpoint_id not in defect_checkpoints
                or not self._usable(item, value)
                or operation.time_quality != "synchronized"
                or operation.finished_at is None
                or value.occurred_at is None
                or value.occurred_at <= operation.finished_at
                or value.operation_run_id != operation.operation_id
                or (value.component_id, value.region_id) != nc.candidate_key[2:4]
                for value in observations
                if value
            ):
                raise InvalidTransition(
                    "Repeat control must follow rework on the defect checkpoint in this cycle."
                )
            if any(
                value.cycle_id == item.cycle_id
                and self._candidate_key(value) == nc.candidate_key
                and value.defect_type is not None
                and value.observation_id not in item.control_replacements
                and not any(
                    o.replaces_observation_id == value.observation_id
                    for o in item.observations.values()
                )
                for value in self._effective_observations(item)
            ):
                raise InvalidTransition("An unresolved defect observation contradicts resolution.")
            item.context_revision += 1
            nc.resolution_status = "resolved"
            nc.review_required = False
            item.review_required = any(
                value.review_required for value in item.nonconformities.values()
            )
            decision = Decision(
                decision_id=str(uuid4()),
                command_type="resolution_decision",
                target_id=nonconformity_id,
                action="resolve",
                reason=reason,
                actor_id=actor_id,
                basis_id=expected_basis_id,
                reviewed_revision=expected_revision,
                context_revision=item.context_revision,
                supersedes_id=nc.resolution_ids[-1] if nc.resolution_ids else None,
                created_at=self.clock(),
                rework_operation_id=rework_operation_id,
                observation_ids=observation_ids,
                control_record_ids=control_record_ids,
            )
            item.decisions.append(decision)
            nc.resolution_ids.append(decision.decision_id)
            self._emit(item)
            return self._decision_view(decision)

        return self._command(
            item.item_id,
            "resolution_decision",
            nonconformity_id,
            idempotency_key,
            payload,
            apply,
        )

    def dispose(
        self,
        item_id: str,
        *,
        action: str,
        reason: str,
        actor_id: str,
        expected_revision: int,
        expected_basis_id: str,
        idempotency_key: str,
    ) -> dict:
        item = self._item(item_id)
        payload = {
            "actor_id": actor_id,
            "action": action,
            "reason": reason,
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
        }

        def apply() -> dict:
            self._check_context(item, expected_revision, expected_basis_id)
            if action not in {"released", "rework", "rejected"} or not reason.strip():
                raise InvalidTransition("Unsupported disposition or empty reason.")
            blockers = self._blockers(item)
            if action == "released" and tuple(
                blocker for blocker in blockers if blocker != "disposition_review_required"
            ):
                raise ReleaseBlocked(blockers)
            if action == "released":
                item.disposition_review_required = False
            item.context_revision += 1
            decision = Decision(
                decision_id=str(uuid4()),
                command_type="item_disposition",
                target_id=item_id,
                action=action,
                reason=reason,
                actor_id=actor_id,
                basis_id=expected_basis_id,
                reviewed_revision=expected_revision,
                context_revision=item.context_revision,
                supersedes_id=item.dispositions[-1].decision_id if item.dispositions else None,
                created_at=self.clock(),
            )
            item.dispositions.append(decision)
            self._emit(item)
            return self._decision_view(decision) | {
                "release_allowed_now": item.release_allowed_now,
                "blockers": item.blockers,
            }

        return self._command(item_id, "item_disposition", item_id, idempotency_key, payload, apply)

    def reopen_nonconformity(
        self,
        nonconformity_id,
        *,
        reason,
        actor_id,
        expected_revision,
        expected_basis_id,
        idempotency_key,
    ):
        item, nc = self._nonconformity(nonconformity_id)
        payload = {
            "reason": reason,
            "actor_id": actor_id,
            "action": "reopen",
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
        }

        def apply():
            self._check_context(item, expected_revision, expected_basis_id, nonconformity_id)
            if nc.status != "confirmed" or nc.resolution_status != "resolved":
                raise InvalidTransition("Only a resolved confirmed defect can be reopened.")
            item.context_revision += 1
            decision = Decision(
                decision_id=str(uuid4()),
                command_type="resolution_decision",
                target_id=nonconformity_id,
                action="reopen",
                reason=reason,
                actor_id=actor_id,
                basis_id=expected_basis_id,
                reviewed_revision=expected_revision,
                context_revision=item.context_revision,
                supersedes_id=nc.resolution_ids[-1],
                created_at=self.clock(),
            )
            nc.resolution_status = "open"
            nc.review_required = False
            nc.resolution_ids.append(decision.decision_id)
            item.decisions.append(decision)
            item.review_required = any(n.review_required for n in item.nonconformities.values())
            self._emit(item)
            return self._decision_view(decision)

        return self._command(
            item.item_id, "resolution_decision", nonconformity_id, idempotency_key, payload, apply
        )

    def acknowledge(self, message_id: str, *, receipt: dict) -> dict:
        if not isinstance(receipt, dict) or type(receipt.get("result_version")) is not int:
            raise AckMismatch("ACK needs an integer result version.")
        with self._lock:
            for item in self._items.values():
                message = item.outbox.get(message_id)
                if message is None:
                    continue
                expected = {
                    key: message.payload[key]
                    for key in (
                        "message_id",
                        "result_version",
                        "producer_id",
                        "external_system",
                        "external_order_id",
                        "external_item_id",
                    )
                }
                if any(receipt.get(key) != value for key, value in expected.items()) or (
                    receipt.get("content_digest") != message.content_digest
                ):
                    raise AckMismatch("The ACK does not describe the stored outbox body.")
                if receipt.get("application_status") not in {"applied", "superseded"}:
                    raise AckMismatch("Unsupported ERP application status.")
                current = receipt.get("current_version_at_accept")
                if (
                    receipt.get("status") not in {"accepted", "duplicate"}
                    or type(current) is not int
                    or current < message.result_version
                    or (
                        receipt["application_status"] == "applied"
                        and current != message.result_version
                    )
                    or (
                        receipt["application_status"] == "superseded"
                        and current <= message.result_version
                    )
                ):
                    raise AckMismatch("Invalid ACK commit state.")
                try:
                    received = datetime.fromisoformat(receipt["received_at"])
                    if received.tzinfo is None or received.utcoffset() is None:
                        raise ValueError
                except (KeyError, TypeError, ValueError) as error:
                    raise AckMismatch("ACK needs the receiver commit timestamp.") from error
                saved = copy.deepcopy(receipt)
                if message.ack is not None:
                    if {k: v for k, v in saved.items() if k != "status"} != {
                        k: v for k, v in message.ack.items() if k != "status"
                    }:
                        raise AckMismatch(
                            "A repeated ACK cannot change the original commit receipt."
                        )
                    return copy.deepcopy(message.ack)
                item.outbox[message_id] = OutboxMessage(
                    **{**message.__dict__, "status": "acknowledged", "ack": saved}
                )
                return copy.deepcopy(saved)
        raise UnknownEntity("Unknown outbox message.")

    def state(self, item_id: str) -> dict:
        item = self._item(item_id)
        with self._lock:
            blockers = self._blockers(item)
            return {
                "item_id": item_id,
                "cycle_id": item.cycle_id,
                "required_checkpoints": item.required_checkpoints,
                "context_revision": item.context_revision,
                "release_allowed_now": not blockers,
                "control_assessments": self._control_assessments(item),
                "context": asdict(item.context) if item.context else None,
                "cycles": copy.deepcopy(item.cycles),
                "production_history": copy.deepcopy(item.production_history),
                "effective_observations": [
                    self._observation_view(o) for o in self._effective_observations(item)
                ],
                "decisions": [self._decision_view(v) for v in item.decisions],
                "dispositions": [self._decision_view(v) for v in item.dispositions],
                "blockers": blockers,
                "review_required": item.review_required,
                "disposition_review_required": item.disposition_review_required,
                "observations": [
                    self._observation_view(value) for value in item.observations.values()
                ],
                "rework_operations": [
                    self._rework_view(value) for value in item.rework_operations.values()
                ],
                "nonconformities": [
                    self._nc_view(value) for value in item.nonconformities.values()
                ],
                "kpi": self._kpi(item),
                "outbox": [self._outbox_view(value) for value in item.outbox.values()],
                "disposition": item.dispositions[-1].action if item.dispositions else "pending",
            }

    def analytics(self, item_ids: Iterable[str] | None = None, *, since=None, until=None) -> dict:
        """Current counts on one explicit cohort; each item contributes at most one."""
        with self._lock:
            ids = tuple(dict.fromkeys(item_ids if item_ids is not None else self._items))
            items = [self._item(item_id) for item_id in ids]
            fields = self._kpi(items[0]) if items else self._kpi(_Item("", "", ()))
            counts = {key: 0 for key in fields}
            for item in items:
                for key, value in self._kpi(item).items():
                    counts[key] += value
            from app.domain.analysis import aggregate

            return {
                "item_ids": ids,
                "items": len(items),
                "kpi": counts,
                **aggregate(items, since=since, until=until),
            }

    def _command(
        self,
        item_id: str,
        command_type: str,
        target_id: str,
        idempotency_key: str,
        payload: dict,
        apply: Callable[[], dict],
    ) -> dict:
        actor = payload.get("actor_id", "")
        _id(actor, "actor_id")
        _id(idempotency_key, "idempotency_key")
        _id(payload.get("reason", ""), "reason")
        key = (actor, command_type, target_id, idempotency_key)
        digest = _digest(payload)
        with self._lock:
            saved = self._commands.get(key)
            if saved is not None:
                if saved[0] != digest:
                    raise IdempotencyConflict(
                        "The idempotency key was used with a different command body."
                    )
                return copy.deepcopy(saved[1])
            item = self._items[item_id]
            before = copy.deepcopy(item)
            try:
                result = apply()
            except Exception:
                self._items[item_id] = before
                raise
            self._commands[key] = (digest, copy.deepcopy(result))
            return result

    def _check_context(
        self,
        item: _Item,
        expected_revision: int,
        expected_basis_id: str,
        target_id: str | None = None,
    ) -> None:
        basis = item.bases.get(expected_basis_id)
        if (
            basis is None
            or basis.item_id != item.item_id
            or basis.target_id != target_id
            or basis.cycle_id != item.cycle_id
        ):
            raise BasisMismatch("The basis does not belong to the requested context or target.")
        if (
            expected_revision != item.context_revision
            or basis.context_revision != item.context_revision
        ):
            raise StaleRevision("The command was prepared against an old context revision.")

    def _emit(self, item: _Item) -> None:
        if self._batch_depth:
            return
        item.blockers = self._blockers(item)
        item.release_allowed_now = not item.blockers
        version = item.next_result_version
        previous = (
            max(item.outbox.values(), key=lambda value: value.result_version).message_id
            if item.outbox
            else None
        )
        message_id = str(uuid4())
        decisions = {value.decision_id: value for value in item.decisions}
        disposition = item.dispositions[-1] if item.dispositions else None

        def decision_snapshot(decision):
            if decision is None:
                return None
            return {
                "decision_id": decision.decision_id,
                "action": decision.action,
                "reason": decision.reason,
                "actor_id": decision.actor_id,
                "basis_id": decision.basis_id,
                "reviewed_revision": decision.reviewed_revision,
            }

        nonconformities = []
        for value in item.nonconformities.values():
            snapshot = self._nc_view(value)
            snapshot["current_decision"] = decision_snapshot(
                decisions.get(value.decision_ids[-1]) if value.decision_ids else None
            )
            snapshot["current_resolution"] = decision_snapshot(
                decisions.get(value.resolution_ids[-1]) if value.resolution_ids else None
            )
            nonconformities.append(snapshot)
        payload = {
            "message_id": message_id,
            **{
                name: getattr(item.context, name) if item.context else None
                for name in (
                    "producer_id",
                    "external_system",
                    "external_order_id",
                    "external_item_id",
                )
            },
            "item_id": item.item_id,
            "result_version": version,
            "previous_message_id": previous,
            "context_revision": item.context_revision,
            "control_cycle_id": item.cycle_id,
            "disposition": disposition.action if disposition else "pending",
            "disposition_decision": decision_snapshot(disposition),
            "release_allowed_now": item.release_allowed_now,
            "blockers": list(item.blockers),
            "review_required": item.review_required or item.disposition_review_required,
            "disposition_review_required": item.disposition_review_required,
            "nonconformities": nonconformities,
            "kpi": self._kpi(item),
        }
        if item.context and item.context.erp_result_profile == "dp01-0.4":
            payload = {
                key: value
                for key, value in payload.items()
                if key not in {"item_id", "nonconformities", "kpi", "disposition_review_required"}
            }
            payload["confirmed_nonconformities"] = [
                {
                    "nonconformity_id": card["nonconformity_id"],
                    "resolution_status": card["resolution_status"],
                    "decision_id": card["current_decision"]["decision_id"],
                    "resolution_id": (
                        card["current_resolution"]["decision_id"]
                        if card["current_resolution"]
                        else None
                    ),
                    "cause_status": card["cause_status"],
                    "cause_category": card["cause_category"],
                    "basis_ids": list(
                        dict.fromkeys(
                            decision["basis_id"]
                            for decision in (card["current_decision"], card["current_resolution"])
                            if decision
                        )
                    ),
                }
                for card in nonconformities
                if card["status"] == "confirmed"
            ]
        body_digest = _digest(payload)
        message = OutboxMessage(
            message_id,
            item.item_id,
            version,
            previous,
            item.context_revision,
            payload,
            body_digest,
            created_at=self.clock(),
        )
        item.outbox[message.message_id] = message
        item.next_result_version += 1

    def _blockers(self, item: _Item) -> tuple[str, ...]:
        blockers: list[str] = []
        if not item.route_known or item.context is None:
            blockers.append("route_unknown")
        if item.context is None:
            blockers.append("production_context_missing")
        else:
            history = item.production_history
            if history.get("work_order_id") != item.context.work_order_id:
                blockers.append("work_order_not_resolved")
            if history.get("assembly_version") != item.context.assembly_version or set(
                history.get("component_ids", ())
            ) != set(item.context.component_ids):
                blockers.append("assembly_not_resolved")
            operations = {
                value["operation_run_id"]: value for value in history.get("operations", ())
            }
            for run in item.context.required_operations:
                if operations.get(run, {}).get("status") != "completed":
                    blockers.append(f"operation_incomplete:{run}")
        blockers.extend(f"scope_constraint:{key}" for key in sorted(item.scope_constraints))
        if any(o.cycle_id is None for o in self._effective_observations(item)):
            blockers.append("observation_cycle_unresolved")
        if item.cycle_id != item.initial_cycle_id and not all(
            run in item.rework_operations and item.rework_operations[run].status == "completed"
            for run in item.cycles[item.cycle_id]["operation_run_ids"]
        ):
            blockers.append("rework_operation_missing")
        for assessment in self._control_assessments(item):
            prefix = {
                "missing": "missing_checkpoint",
                "not_assessable": "not_assessable",
                "disputed": "disputed_checkpoint",
            }.get(assessment["assessment_status"])
            if prefix:
                blockers.append(f"{prefix}:{assessment['checkpoint_id']}")
        for nc in item.nonconformities.values():
            if nc.status in {"pending_review", "needs_check"}:
                blockers.append(f"nonconformity:{nc.nonconformity_id}:{nc.status}")
            elif nc.status == "confirmed" and nc.resolution_status == "open":
                blockers.append(f"nonconformity:{nc.nonconformity_id}:open")
            if nc.review_required:
                blockers.append(f"review_required:{nc.nonconformity_id}")
        if item.review_required:
            blockers.append("context_review_required")
        if item.disposition_review_required:
            blockers.append("disposition_review_required")
        return tuple(dict.fromkeys(blockers))

    def _kpi(self, item: _Item) -> dict:
        confirmed = [nc for nc in item.nonconformities.values() if nc.status == "confirmed"]
        return {
            "inspected_items": int(
                any(self._usable(item, observation) for observation in item.observations.values())
            ),
            "confirmed_nonconforming_items": int(bool(confirmed)),
            "confirmed_defects": len(confirmed),
            "open_confirmed_defects": sum(nc.resolution_status == "open" for nc in confirmed),
            "resolved_confirmed_defects": sum(
                nc.resolution_status == "resolved" for nc in confirmed
            ),
            "open_nonconforming_items": int(
                any(nc.status == "confirmed" and nc.resolution_status == "open" for nc in confirmed)
            ),
            "review_required_items": int(
                item.review_required
                or item.disposition_review_required
                or any(nc.review_required for nc in item.nonconformities.values())
            ),
            "unresolved_cause_defects": sum(nc.cause_status != "established" for nc in confirmed),
            "established_cause_defects": sum(nc.cause_status == "established" for nc in confirmed),
            "confirmed_operator_errors": len(
                {
                    d.operation_run_id
                    for d in item.decisions
                    if d.decision_id
                    in {
                        nc.decision_ids[-1]
                        for nc in confirmed
                        if nc.decision_ids
                        and nc.cause_status == "established"
                        and nc.cause_category == "operator"
                    }
                }
            ),
            "rework_operations": sum(
                operation.status == "completed" for operation in item.rework_operations.values()
            ),
            "current_cycle_inspected_items": int(
                any(
                    o.cycle_id == item.cycle_id and self._usable(item, o)
                    for o in self._effective_observations(item)
                )
            ),
            "repeat_controls": len(
                {
                    observation.record_id or observation.observation_id
                    for observation in self._effective_observations(item)
                    if observation.cycle_id is not None
                    and (
                        observation.cycle_id != item.initial_cycle_id
                        or observation.replaces_observation_id is not None
                        or observation.observation_id in item.control_replacements.values()
                    )
                }
            ),
            "blocked_items": int(bool(self._blockers(item))),
            "released_after_reinspection": int(
                bool(item.dispositions)
                and item.dispositions[-1].action == "released"
                and any(
                    operation.status == "completed" for operation in item.rework_operations.values()
                )
                and not self._blockers(item)
            ),
        }

    def _item(self, item_id: str) -> _Item:
        try:
            return self._items[item_id]
        except KeyError as error:
            raise UnknownEntity("Unknown item.") from error

    def _nonconformity(self, nonconformity_id: str) -> tuple[_Item, Nonconformity]:
        for item in self._items.values():
            if nonconformity_id in item.nonconformities:
                return item, item.nonconformities[nonconformity_id]
        raise UnknownEntity("Unknown nonconformity.")

    def _validate_cause(
        self,
        item,
        *,
        action,
        basis_id,
        cause_status,
        cause_category,
        evidence_record_ids,
        operation_run_id,
        operator_id,
        action_record_id,
    ):
        if cause_status not in {"unresolved", "established"}:
            raise InvalidTransition("A human decision cannot store an automatic hypothesis.")
        if cause_category not in {"incoming", "equipment", "operator", "other", "unknown"}:
            raise InvalidTransition("Unsupported cause category.")
        if cause_status == "unresolved":
            if cause_category != "unknown" or evidence_record_ids:
                raise InvalidTransition("An unresolved cause cannot claim a category or evidence.")
            return
        if action != "confirm" or cause_category == "unknown" or not evidence_record_ids:
            raise InvalidTransition("An established cause needs a confirmed defect and evidence.")
        basis = item.bases[basis_id]
        if not set(evidence_record_ids).issubset(basis.evidence_record_ids):
            raise InvalidTransition("Cause evidence must belong to the reviewed basis.")
        timeline = item.production_history.get("timeline", ())
        by_record = {entry["record_id"]: entry for entry in timeline}
        if any(record not in by_record for record in evidence_record_ids):
            raise InvalidTransition("Cause evidence is unavailable in the production history.")
        if cause_category == "operator":
            operation = next(
                (
                    value
                    for value in item.production_history.get("operations", ())
                    if value["operation_run_id"] == operation_run_id
                ),
                None,
            )
            action_entry = by_record.get(action_record_id)
            if (
                operation is None
                or operation.get("operator_id") != operator_id
                or action_entry is None
                or action_entry.get("event_type") != "operator_action"
                or action_record_id not in evidence_record_ids
                or operation_run_id != action_entry.get("operation_run_id")
                or operator_id != action_entry.get("operator_id")
            ):
                raise InvalidTransition(
                    "An operator cause needs the reviewed operation, operator and action evidence."
                )

    @staticmethod
    def _basis_view(value: Basis) -> dict:
        return copy.deepcopy({**value.__dict__, "created_at": value.created_at.isoformat()})

    @staticmethod
    def _decision_view(value: Decision) -> dict:
        return {
            **value.__dict__,
            "observation_ids": list(value.observation_ids),
            "cause_evidence_record_ids": list(value.cause_evidence_record_ids),
            "control_record_ids": list(value.control_record_ids),
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def _observation_view(value: Observation) -> dict:
        return {
            **value.__dict__,
            "created_at": value.created_at.isoformat(),
            "occurred_at": value.occurred_at.isoformat() if value.occurred_at else None,
        }

    @staticmethod
    def _rework_view(value: ReworkOperation) -> dict:
        return {**value.__dict__, "created_at": value.created_at.isoformat()}

    @staticmethod
    def _nc_view(value: Nonconformity) -> dict:
        return {
            **value.__dict__,
            "decision_ids": list(value.decision_ids),
            "resolution_ids": list(value.resolution_ids),
        }

    @staticmethod
    def _outbox_view(value: OutboxMessage) -> dict:
        return copy.deepcopy(value.__dict__)

    @staticmethod
    def _candidate_key(value: Observation) -> tuple:
        scope = (value.source_id, value.item_id, value.component_id, value.region_id)
        if value.defect_ref and value.source_id:
            return (*scope, value.defect_ref)
        return (*scope, None, value.observation_id)

    @classmethod
    def _control_assessments(cls, item: _Item) -> tuple[dict, ...]:
        if item.context is None:
            return ()
        assessments = []
        for requirement in item.context.controls:
            current = [
                value
                for value in cls._effective_observations(item)
                if value.cycle_id == item.cycle_id
                and value.checkpoint_id == requirement.checkpoint_id
            ]
            active = [
                value
                for value in current
                if value.observation_id not in item.control_replacements
                and not any(
                    successor.replaces_observation_id == value.observation_id
                    for successor in current
                )
            ]
            usable = [value for value in active if cls._usable(item, value)]
            rejected = {
                value.observation_id: nc
                for value in usable
                for nc in item.nonconformities.values()
                if nc.candidate_key == cls._candidate_key(value)
                and nc.status == "rejected"
                and not nc.review_required
            }
            results = {
                value.effective_result for value in usable if value.observation_id not in rejected
            }
            if not current:
                status = "missing"
            elif len(usable) != len(active) or not usable:
                status = "not_assessable"
            elif {"defect_signs", "no_defect_signs"}.issubset(results):
                status = "disputed"
            else:
                status = "assessable"
            assessments.append(
                {
                    "checkpoint_id": requirement.checkpoint_id,
                    "requirement_version": (
                        f"{item.context.route_version}:{item.context.rules_version}:"
                        f"{requirement.analyzer_version}"
                    ),
                    "cycle_id": item.cycle_id,
                    "component_id": requirement.component_id,
                    "region_id": requirement.region_id,
                    "inspection_phase": requirement.inspection_phase,
                    "operation_run_id": item.cycles[item.cycle_id]
                    .get("checkpoint_operation_run_ids", {})
                    .get(requirement.checkpoint_id),
                    "control_session_id": item.cycles[item.cycle_id]
                    .get("control_sessions", {})
                    .get(requirement.checkpoint_id),
                    "assessment_status": status,
                    "effective_results": sorted(results),
                    "resolution_decision_ids": [nc.decision_ids[-1] for nc in rejected.values()],
                    "selected_record_ids": [value.record_id for value in usable],
                    "contradicting_record_ids": (
                        [value.record_id for value in usable] if status == "disputed" else []
                    ),
                    "excluded_inputs": [
                        {
                            "observation_id": value.observation_id,
                            "record_id": value.record_id,
                            "reasons": cls._exclusion_reasons(item, requirement, value),
                        }
                        for value in current
                        if value not in usable
                    ],
                }
            )
        return tuple(assessments)

    @staticmethod
    def _exclusion_reasons(item, requirement, value):
        reasons = []
        if value.observation_id in item.control_replacements or any(
            successor.replaces_observation_id == value.observation_id
            for successor in item.observations.values()
        ):
            reasons.append("superseded_by_repeat_control")
        if value.quality != "good" or value.effective_result == "not_assessable":
            reasons.append("observation_not_assessable")
        if value.identification_status != "reliable":
            reasons.append("identification_unreliable")
        if value.time_quality != "synchronized" or value.occurred_at is None:
            reasons.append("time_unreliable")
        if value.record_id is None:
            reasons.append("source_record_missing")
        plan = item.cycles.get(value.cycle_id, {})
        if value.control_session_id != plan.get("control_sessions", {}).get(value.checkpoint_id):
            reasons.append("control_session_unresolved")
        if value.operation_run_id != plan.get("checkpoint_operation_run_ids", {}).get(
            value.checkpoint_id
        ):
            reasons.append("operation_binding_mismatch")
        if value.inspection_phase != requirement.inspection_phase:
            reasons.append("inspection_phase_mismatch")
        if (value.source_id, value.analyzer_version) != (
            requirement.source_id,
            requirement.analyzer_version,
        ):
            reasons.append("method_not_applicable")
        if (value.component_id, value.region_id) != (
            requirement.component_id,
            requirement.region_id,
        ):
            reasons.append("control_scope_mismatch")
        if not reasons and not QualityWorkflow._usable(item, value):
            reasons.append("operation_time_not_applicable")
        return reasons

    @staticmethod
    def _usable(item: _Item, value: Observation) -> bool:
        value = QualityWorkflow._effective_observation(item, value)
        if (
            item.context is None
            or value.cycle_id is None
            or value.quality != "good"
            or value.effective_result == "not_assessable"
            or value.identification_status != "reliable"
            or value.time_quality != "synchronized"
            or value.occurred_at is None
            or value.record_id is None
        ):
            return False
        rule = next(
            (r for r in item.context.controls if r.checkpoint_id == value.checkpoint_id),
            None,
        )
        if rule is None:
            return False
        plan = item.cycles.get(value.cycle_id)
        if plan is None or (
            value.control_session_id != plan.get("control_sessions", {}).get(value.checkpoint_id)
            or value.operation_run_id
            != plan.get("checkpoint_operation_run_ids", {}).get(value.checkpoint_id)
            or value.inspection_phase != rule.inspection_phase
            or (
                value.source_id,
                value.analyzer_version,
                value.component_id,
                value.region_id,
            )
            != (rule.source_id, rule.analyzer_version, rule.component_id, rule.region_id)
        ):
            return False
        if value.operation_run_id is None:
            return rule.inspection_phase == "incoming"
        operation = next(
            (
                part
                for part in item.production_history.get("operations", ())
                if part["operation_run_id"] == value.operation_run_id
            ),
            None,
        )
        if operation is None and value.operation_run_id in item.rework_operations:
            rework = item.rework_operations[value.operation_run_id]
            operation = {
                "operation_run_id": rework.operation_id,
                "status": rework.status,
                "started_at": None,
                "finished_at": rework.finished_at.isoformat() if rework.finished_at else None,
                "issues": (
                    [] if rework.time_quality == "synchronized" else ["uncertain_source_time"]
                ),
            }
        if operation is None or operation.get("status") != "completed":
            return False
        if "uncertain_source_time" in operation.get("issues", ()):
            return False
        if rule.inspection_phase in {"after_operation", "final"}:
            boundary = operation.get("finished_at")
            return bool(boundary and value.occurred_at >= datetime.fromisoformat(boundary))
        if rule.inspection_phase == "before_operation":
            boundary = operation.get("started_at")
            return bool(boundary and value.occurred_at <= datetime.fromisoformat(boundary))
        return False

    def set_production_history(
        self, item_id: str, history: dict, record_ids: Iterable[str]
    ) -> None:
        """Internal projection hook; callers authenticate raw before invoking it."""
        with self._lock:
            item = self._item(item_id)
            history = copy.deepcopy(history)
            ids = list(dict.fromkeys(record_ids))
            if item.production_history == history and item.evidence_record_ids == ids:
                return
            item.production_history = history
            item.evidence_record_ids = ids
            item.context_revision += 1
            if item.dispositions:
                item.disposition_review_required = True
            for nc in item.nonconformities.values():
                if nc.decision_ids:
                    nc.review_required = True
            item.review_required = any(nc.review_required for nc in item.nonconformities.values())
            self._emit(item)

    def project_source_fact(
        self, item_id: str, document: dict, record_id: str, history: dict, occurred_at: datetime
    ) -> tuple[str, tuple[str, ...]]:
        """One authenticated source fact, one context revision and one outbox snapshot."""
        with self._lock:
            item = self._item(item_id)
            before = copy.deepcopy(item)
            self._batch_depth += 1
            issues = []
            try:
                self.set_production_history(
                    item_id, history, [*item.evidence_record_ids, record_id]
                )
                payload = document["payload"]
                run = document.get("operation_run_id")
                if item.context and not item.context.authorizes(
                    document["source_id"], document["event_type"], document.get("station_id")
                ):
                    raise PermissionDenied(
                        "This source cannot assert the supplied production fact."
                    )
                cycle = next(
                    (key for key, plan in item.cycles.items() if run in plan["operation_run_ids"]),
                    None,
                )
                if item.context and run in item.context.required_operations:
                    cycle = item.initial_cycle_id
                if document["event_type"] == "inspection_observed":
                    checkpoint = payload["checkpoint_id"]
                    session = payload.get("control_session_id")
                    claimed = payload.get("control_cycle_id")
                    matches = [
                        key
                        for key, plan in item.cycles.items()
                        if plan.get("control_sessions", {}).get(checkpoint) == session
                        and plan.get("checkpoint_operation_run_ids", {}).get(checkpoint) == run
                    ]
                    cycle = matches[0] if len(matches) == 1 else None
                    if claimed is not None and claimed != cycle:
                        cycle = None
                    if cycle is None:
                        issues.append("observation_cycle_unresolved")
                    rule = (
                        next(
                            (
                                value
                                for value in item.context.controls
                                if value.checkpoint_id == checkpoint
                            ),
                            None,
                        )
                        if item.context
                        else None
                    )
                    for index, defect in enumerate(payload["defects"] or [None]):
                        sign = defect or {}
                        self.record_observation(
                            item_id,
                            observation_id=f"{record_id}:{index}",
                            checkpoint_id=payload["checkpoint_id"],
                            result=payload["inspection_result"],
                            quality=payload["observation_quality"],
                            defect_ref=sign.get("defect_ref"),
                            defect_type=sign.get("type"),
                            source_event_id=document["event_id"],
                            cycle_id=cycle,
                            replaces_observation_id=payload.get("replaces_observation_id"),
                            source_id=document["source_id"],
                            component_id=sign.get("component_id")
                            or (rule.component_id if rule else None),
                            region_id=sign.get("region_id") or (rule.region_id if rule else None),
                            analyzer_version=payload.get("analyzer_version"),
                            operation_run_id=run,
                            record_id=record_id,
                            occurred_at=occurred_at,
                            time_quality=document.get("time_quality", "uncertain"),
                            identification_status=document.get(
                                "identification_status", "uncertain"
                            ),
                            inspection_phase=payload.get("inspection_phase"),
                            control_session_id=session,
                            defect_index=index if defect is not None else None,
                        )
                if document["event_type"] in {"operation_started", "operation_finished"}:
                    operation = next(
                        (
                            op
                            for op in history.get("operations", ())
                            if op["operation_run_id"] == run
                        ),
                        None,
                    )
                    if cycle is not None and cycle != item.initial_cycle_id and operation:
                        if operation["status"] in {"completed", "in_progress", "invalid"}:
                            self.record_rework_operation(
                                item_id,
                                operation_id=run,
                                cycle_id=cycle,
                                previous_operation_run_id=operation["previous_operation_run_id"],
                                status=operation["status"],
                                finished_at=(
                                    datetime.fromisoformat(operation["finished_at"])
                                    if operation.get("finished_at")
                                    else None
                                ),
                                time_quality=(
                                    "uncertain"
                                    if "uncertain_source_time" in operation.get("issues", ())
                                    else "synchronized"
                                ),
                            )
                item.context_revision = before.context_revision + 1
                if item.context is None:
                    issues.append("work_order_not_resolved")
            except Exception:
                self._items[item_id] = before
                raise
            finally:
                self._batch_depth -= 1
            self._emit(item)
            return ("unresolved" if issues else "applied", tuple(issues))


class InMemoryErpReceiver:
    """In-memory protocol oracle, not the separate durable ERP emulator."""

    def __init__(self) -> None:
        self.messages: dict[str, tuple[str, dict]] = {}
        self.stream_versions: dict[tuple, int] = {}
        self.version_messages: dict[tuple, str] = {}
        self.current_payloads: dict[tuple, dict] = {}
        self.history: list[dict] = []

    def receive(self, message: OutboxMessage) -> dict:
        digest = _digest(message.payload)
        if (
            digest != message.content_digest
            or message.payload.get("message_id") != message.message_id
            or message.payload.get("result_version") != message.result_version
        ):
            raise AckMismatch("Invalid message body or digest.")
        fields = ("producer_id", "external_system", "external_order_id", "external_item_id")
        stream = tuple(message.payload.get(key) for key in fields)
        if any(not isinstance(value, str) or not value.strip() for value in stream):
            raise AckMismatch("The external stream mapping is missing.")
        existing = self.messages.get(message.message_id)
        if existing is not None:
            if existing[0] != digest:
                raise AckMismatch("The message ID was reused with a different body.")
            return copy.deepcopy(existing[1]) | {"status": "duplicate"}
        current = self.stream_versions.get(stream, 0)
        application_status = "applied" if message.result_version > current else "superseded"
        version_key = (*stream, message.result_version)
        if version_key in self.version_messages:
            raise IdempotencyConflict("The same stream/version has a different message ID.")
        receipt = {
            "message_id": message.message_id,
            "result_version": message.result_version,
            "application_status": application_status,
            **dict(zip(fields, stream, strict=True)),
            "content_digest": digest,
            "received_at": _now().isoformat(),
            "current_version_at_accept": max(current, message.result_version),
        }
        self.messages[message.message_id] = (digest, receipt)
        self.version_messages[version_key] = message.message_id
        self.history.append(
            {
                "stream": stream,
                "result_version": message.result_version,
                "message_id": message.message_id,
            }
        )
        if application_status == "applied":
            self.stream_versions[stream] = message.result_version
            self.current_payloads[stream] = copy.deepcopy(message.payload)
        return copy.deepcopy(receipt) | {"status": "accepted"}
