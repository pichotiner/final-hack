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
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from uuid import uuid4

from app.domain.errors import (
    AckMismatch,
    BasisMismatch,
    IdempotencyConflict,
    InvalidTransition,
    ReleaseBlocked,
    StaleRevision,
    UnknownEntity,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _digest(value: object) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


@dataclass(frozen=True)
class Observation:
    observation_id: str
    item_id: str
    cycle_id: str
    checkpoint_id: str
    result: str
    effective_result: str
    quality: str
    defect_ref: str | None
    defect_type: str | None
    source_event_id: str | None
    created_at: datetime


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


@dataclass
class Nonconformity:
    nonconformity_id: str
    item_id: str
    candidate_key: tuple[str, ...]
    status: str = "pending_review"
    resolution_status: str = "not_applicable"
    review_required: bool = False
    decision_ids: list[str] = field(default_factory=list)
    resolution_ids: list[str] = field(default_factory=list)


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
    route_known: bool = True
    context_revision: int = 0
    observations: dict[str, Observation] = field(default_factory=dict)
    nonconformities: dict[str, Nonconformity] = field(default_factory=dict)
    bases: dict[str, Basis] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    dispositions: list[Decision] = field(default_factory=list)
    outbox: dict[str, OutboxMessage] = field(default_factory=dict)
    next_result_version: int = 1
    release_allowed_now: bool = False
    blockers: tuple[str, ...] = ()
    review_required: bool = False


class QualityWorkflow:
    """Atomic reference implementation of the role №1 P0 command semantics."""

    def __init__(self, clock: Callable[[], datetime] = _now) -> None:
        self.clock = clock
        self._items: dict[str, _Item] = {}
        self._commands: dict[tuple[str, str, str, str], tuple[str, dict]] = {}
        self._lock = RLock()

    def open_item(
        self,
        item_id: str,
        *,
        required_checkpoints: Iterable[str] = ("incoming", "final"),
        cycle_id: str = "cycle-1",
        route_known: bool = True,
    ) -> None:
        item_id = _id(item_id, "item_id")
        checkpoints = tuple(
            dict.fromkeys(_id(value, "checkpoint_id") for value in required_checkpoints)
        )
        if not checkpoints:
            raise ValueError("At least one required checkpoint is needed.")
        with self._lock:
            if item_id in self._items:
                raise ValueError("The item is already open.")
            self._items[item_id] = _Item(item_id, cycle_id, checkpoints, route_known=route_known)

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
    ) -> dict:
        """Open the explicitly assigned cycle after a rework disposition."""
        item = self._item(item_id)
        payload = {
            "actor_id": actor_id,
            "cycle_id": cycle_id,
            "required_checkpoints": tuple(required_checkpoints or item.required_checkpoints),
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
            "reason": reason,
        }

        def apply() -> dict:
            self._check_context(item, expected_revision, expected_basis_id)
            if item.dispositions and item.dispositions[-1].action != "rework":
                raise InvalidTransition("A new cycle requires the latest disposition to be rework.")
            if cycle_id == item.cycle_id:
                raise InvalidTransition("The new control cycle must have a new ID.")
            item.cycle_id = _id(cycle_id, "cycle_id")
            item.required_checkpoints = tuple(payload["required_checkpoints"])
            item.context_revision += 1
            item.review_required = False
            result = {"cycle_id": item.cycle_id, "context_revision": item.context_revision}
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
    ) -> dict:
        """Attach one trusted observation and create/update its defect candidate."""
        item = self._item(item_id)
        observation_id = _id(observation_id, "observation_id")
        checkpoint_id = _id(checkpoint_id, "checkpoint_id")
        if result not in {"defect_signs", "no_defect_signs", "not_assessable"}:
            raise ValueError("Unsupported inspection result.")
        if quality not in {"good", "poor", "unknown"}:
            raise ValueError("Unsupported observation quality.")
        if result == "defect_signs" and (not defect_ref or not defect_type):
            raise ValueError(
                "defect_signs requires defect_ref and defect_type in the demonstrator."
            )
        if result == "no_defect_signs" and quality in {"poor", "unknown"}:
            effective = "not_assessable"
        else:
            effective = result
        with self._lock:
            if observation_id in item.observations:
                saved = item.observations[observation_id]
                return {
                    "status": "duplicate",
                    "observation_id": saved.observation_id,
                    "context_revision": item.context_revision,
                }
            observation = Observation(
                observation_id=observation_id,
                item_id=item_id,
                cycle_id=cycle_id or item.cycle_id,
                checkpoint_id=checkpoint_id,
                result=result,
                effective_result=effective,
                quality=quality,
                defect_ref=defect_ref,
                defect_type=defect_type,
                source_event_id=source_event_id,
                created_at=self.clock(),
            )
            item.observations[observation_id] = observation
            if effective == "defect_signs":
                candidate_key = (item_id, observation.cycle_id, defect_ref or observation_id)
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

    def create_basis(self, item_id: str, target_id: str | None = None) -> dict:
        item = self._item(item_id)
        with self._lock:
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
            )
            item.bases[basis.basis_id] = basis
            return self._basis_view(basis)

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
    ) -> dict:
        item, nc = self._nonconformity(nonconformity_id)
        payload = {
            "actor_id": actor_id,
            "action": action,
            "reason": reason,
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
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
                raise InvalidTransition("Only a pending nonconformity can be confirmed.")
            if action == "reject" and nc.status == "rejected":
                raise InvalidTransition("The nonconformity is already rejected.")
            old = item.decisions[-1].decision_id if item.decisions else None
            item.context_revision += 1
            nc.status = {
                "confirm": "confirmed",
                "reject": "rejected",
                "request_check": "needs_check",
            }[action]
            nc.resolution_status = "open" if action == "confirm" else "not_applicable"
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
    ) -> dict:
        item, nc = self._nonconformity(nonconformity_id)
        observation_ids = tuple(observation_ids)
        payload = {
            "actor_id": actor_id,
            "action": "resolve",
            "reason": reason,
            "expected_revision": expected_revision,
            "expected_basis_id": expected_basis_id,
            "rework_operation_id": rework_operation_id,
            "previous_operation_run_id": previous_operation_run_id,
            "observation_ids": observation_ids,
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
            observations = [item.observations.get(value) for value in observation_ids]
            if not observations or any(value is None for value in observations):
                raise InvalidTransition("Resolution requires existing repeat-control observations.")
            if any(
                value.effective_result != "no_defect_signs" or value.quality != "good"
                for value in observations
                if value
            ):
                raise InvalidTransition("Resolution requires a good repeat control.")
            item.context_revision += 1
            nc.resolution_status = "resolved"
            nc.review_required = False
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
            if action == "released" and blockers:
                raise ReleaseBlocked(blockers)
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

    def acknowledge(
        self, message_id: str, *, result_version: int, content_digest: str, application_status: str
    ) -> dict:
        with self._lock:
            for item in self._items.values():
                message = item.outbox.get(message_id)
                if message is None:
                    continue
                if (
                    message.result_version != result_version
                    or message.content_digest != content_digest
                ):
                    raise AckMismatch("The ACK does not describe the stored outbox body.")
                if application_status not in {"applied", "superseded"}:
                    raise AckMismatch("Unsupported ERP application status.")
                saved = {
                    "message_id": message_id,
                    "result_version": result_version,
                    "application_status": application_status,
                }
                item.outbox[message_id] = OutboxMessage(
                    **{**message.__dict__, "status": "acknowledged", "ack": saved}
                )
                return saved
        raise UnknownEntity("Unknown outbox message.")

    def state(self, item_id: str) -> dict:
        item = self._item(item_id)
        with self._lock:
            blockers = self._blockers(item)
            return {
                "item_id": item_id,
                "cycle_id": item.cycle_id,
                "context_revision": item.context_revision,
                "release_allowed_now": not blockers,
                "blockers": blockers,
                "review_required": item.review_required,
                "observations": [
                    self._observation_view(value) for value in item.observations.values()
                ],
                "nonconformities": [
                    self._nc_view(value) for value in item.nonconformities.values()
                ],
                "kpi": self._kpi(item),
                "outbox": [self._outbox_view(value) for value in item.outbox.values()],
                "disposition": item.dispositions[-1].action if item.dispositions else "pending",
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
            or (target_id and basis.target_id not in {None, target_id})
        ):
            raise BasisMismatch("The basis does not belong to the requested context or target.")
        if (
            expected_revision != item.context_revision
            or basis.context_revision != item.context_revision
        ):
            raise StaleRevision("The command was prepared against an old context revision.")

    def _emit(self, item: _Item) -> None:
        item.blockers = self._blockers(item)
        item.release_allowed_now = not item.blockers
        version = item.next_result_version
        previous = next(reversed(item.outbox.values())).message_id if item.outbox else None
        payload = {
            "item_id": item.item_id,
            "result_version": version,
            "previous_message_id": previous,
            "context_revision": item.context_revision,
            "control_cycle_id": item.cycle_id,
            "disposition": item.dispositions[-1].action if item.dispositions else "pending",
            "release_allowed_now": item.release_allowed_now,
            "blockers": list(item.blockers),
            "review_required": item.review_required,
            "nonconformities": [self._nc_view(value) for value in item.nonconformities.values()],
            "kpi": self._kpi(item),
        }
        body_digest = _digest(payload)
        message = OutboxMessage(
            str(uuid4()),
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
        if not item.route_known:
            blockers.append("route_unknown")
        for checkpoint in item.required_checkpoints:
            current = [
                value
                for value in item.observations.values()
                if value.cycle_id == item.cycle_id and value.checkpoint_id == checkpoint
            ]
            if not current:
                blockers.append(f"missing_checkpoint:{checkpoint}")
            elif current[-1].effective_result == "not_assessable":
                blockers.append(f"not_assessable:{checkpoint}")
        for nc in item.nonconformities.values():
            if nc.status in {"pending_review", "needs_check"}:
                blockers.append(f"nonconformity:{nc.nonconformity_id}:{nc.status}")
            elif nc.status == "confirmed" and nc.resolution_status == "open":
                blockers.append(f"nonconformity:{nc.nonconformity_id}:open")
            if nc.review_required:
                blockers.append(f"review_required:{nc.nonconformity_id}")
        if item.review_required:
            blockers.append("context_review_required")
        return tuple(dict.fromkeys(blockers))

    def _kpi(self, item: _Item) -> dict:
        confirmed = [nc for nc in item.nonconformities.values() if nc.status == "confirmed"]
        return {
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
                or any(nc.review_required for nc in item.nonconformities.values())
            ),
            "rework_operations": sum(decision.action == "resolve" for decision in item.decisions),
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

    @staticmethod
    def _basis_view(value: Basis) -> dict:
        return {**value.__dict__, "created_at": value.created_at.isoformat()}

    @staticmethod
    def _decision_view(value: Decision) -> dict:
        return {
            **value.__dict__,
            "observation_ids": list(value.observation_ids),
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def _observation_view(value: Observation) -> dict:
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
        return {**value.__dict__}


class InMemoryErpReceiver:
    """Persistent-semantics reference receiver used by the M0 test."""

    def __init__(self) -> None:
        self.messages: dict[str, tuple[str, dict]] = {}
        self.stream_versions: dict[str, int] = {}
        self.history: list[dict] = []

    def receive(self, message: OutboxMessage) -> dict:
        digest = message.content_digest
        existing = self.messages.get(message.message_id)
        if existing is not None:
            if existing[0] != digest:
                raise AckMismatch("The message ID was reused with a different body.")
            return copy.deepcopy(existing[1]) | {"status": "duplicate"}
        stream = message.item_id
        current = self.stream_versions.get(stream, 0)
        application_status = "applied" if message.result_version > current else "superseded"
        if message.result_version <= current and message.result_version == current:
            for saved in self.history:
                if saved["item_id"] == stream and saved["result_version"] == message.result_version:
                    raise IdempotencyConflict("The same stream/version has a different message ID.")
        receipt = {
            "message_id": message.message_id,
            "result_version": message.result_version,
            "application_status": application_status,
        }
        self.messages[message.message_id] = (digest, receipt)
        self.history.append(
            {
                "item_id": stream,
                "result_version": message.result_version,
                "message_id": message.message_id,
            }
        )
        if application_status == "applied":
            self.stream_versions[stream] = message.result_version
        return copy.deepcopy(receipt) | {"status": "accepted"}
