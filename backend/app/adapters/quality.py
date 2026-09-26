"""PostgreSQL persistence for the executable DP-01 quality workflow."""

from __future__ import annotations

import copy
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from app.adapters.locks import production_guard
from app.adapters.postgres import (
    quality_bases,
    quality_commands,
    quality_constraints,
    quality_decisions,
    quality_deliveries,
    quality_items,
    quality_outbox,
    quality_state_versions,
)
from app.domain.control import decode_context
from app.domain.errors import (
    IdempotencyConflict,
    IntegrityViolation,
    InvalidTransition,
    UnknownEntity,
)
from app.domain.quality import (
    Basis,
    Decision,
    Nonconformity,
    Observation,
    OutboxMessage,
    QualityWorkflow,
    ReworkOperation,
    _digest,
)

_command_document = ContextVar("command_document", default=None)


class CommandResult(dict):
    """Original result plus transport metadata, never persisted inside the result."""

    def __init__(self, result, *, duplicate):
        super().__init__(result)
        self.duplicate = duplicate


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _encode(item) -> dict:
    def encode(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, tuple):
            return [encode(part) for part in value]
        if isinstance(value, list):
            return [encode(part) for part in value]
        if isinstance(value, dict):
            return {key: encode(part) for key, part in value.items()}
        if hasattr(value, "__dict__"):
            return encode(asdict(value))
        return value

    return encode(item)


def _compact(item):
    state = _encode(item)
    state.update(
        storage_format=2,
        decision_refs=[d.decision_id for d in item.decisions],
        disposition_refs=[d.decision_id for d in item.dispositions],
        basis_refs=[
            b.basis_id for b in item.bases.values() if b.context_revision == item.context_revision
        ],
    )
    state.update(bases={}, decisions=[], dispositions=[], outbox={})
    return state


def _decode(item: dict):
    from app.domain.quality import _Item

    observations = {
        key: Observation(
            **{
                **value,
                "created_at": _datetime(value["created_at"]),
                "occurred_at": _datetime(value["occurred_at"])
                if value.get("occurred_at")
                else None,
            }
        )
        for key, value in item["observations"].items()
    }
    rework_operations = {
        key: ReworkOperation(
            operation_id=value["operation_id"],
            item_id=value["item_id"],
            cycle_id=value["cycle_id"],
            previous_operation_run_id=value["previous_operation_run_id"],
            status=value["status"],
            created_at=_datetime(value["created_at"]),
            finished_at=_datetime(value["finished_at"]) if value.get("finished_at") else None,
            time_quality=value.get("time_quality", "uncertain"),
        )
        for key, value in item["rework_operations"].items()
    }
    nonconformities = {
        key: Nonconformity(
            nonconformity_id=value["nonconformity_id"],
            item_id=value["item_id"],
            candidate_key=tuple(value["candidate_key"]),
            status=value["status"],
            resolution_status=value["resolution_status"],
            review_required=value["review_required"],
            decision_ids=list(value["decision_ids"]),
            resolution_ids=list(value["resolution_ids"]),
            cause_status=value.get("cause_status", "unresolved"),
            cause_category=value.get("cause_category", "unknown"),
            cause_evidence_record_ids=tuple(value.get("cause_evidence_record_ids", ())),
            cause_limitations=tuple(value.get("cause_limitations", ("cause_not_established",))),
        )
        for key, value in item["nonconformities"].items()
    }
    bases = {
        key: Basis(
            basis_id=value["basis_id"],
            item_id=value["item_id"],
            target_id=value["target_id"],
            context_revision=value["context_revision"],
            cycle_id=value["cycle_id"],
            observation_ids=tuple(value["observation_ids"]),
            nonconformity_ids=tuple(value["nonconformity_ids"]),
            blockers=tuple(value["blockers"]),
            created_at=_datetime(value["created_at"]),
            context_snapshot=value.get("context_snapshot", {}),
            evidence_record_ids=tuple(value.get("evidence_record_ids", ())),
            decision_ids=tuple(value.get("decision_ids", ())),
            analysis_snapshot=value.get("analysis_snapshot", {}),
            processor_version=value.get("processor_version", "quality-v1"),
            control_assessments=tuple(value.get("control_assessments", ())),
        )
        for key, value in item["bases"].items()
    }

    def decision(value):
        return Decision(
            decision_id=value["decision_id"],
            command_type=value["command_type"],
            target_id=value["target_id"],
            action=value["action"],
            reason=value["reason"],
            actor_id=value["actor_id"],
            basis_id=value["basis_id"],
            reviewed_revision=value["reviewed_revision"],
            context_revision=value["context_revision"],
            supersedes_id=value["supersedes_id"],
            observation_ids=tuple(value["observation_ids"]),
            created_at=_datetime(value["created_at"]),
            rework_operation_id=value["rework_operation_id"],
            cause_status=value.get("cause_status", "unresolved"),
            cause_category=value.get("cause_category", "unknown"),
            cause_evidence_record_ids=tuple(value.get("cause_evidence_record_ids", ())),
            operation_run_id=value.get("operation_run_id"),
            operator_id=value.get("operator_id"),
            action_record_id=value.get("action_record_id"),
            control_record_ids=tuple(value.get("control_record_ids", ())),
            control_binding=value.get("control_binding", {}),
        )

    def message(value):
        return OutboxMessage(
            message_id=value["message_id"],
            item_id=value["item_id"],
            result_version=value["result_version"],
            previous_message_id=value["previous_message_id"],
            context_revision=value["context_revision"],
            payload=value["payload"],
            content_digest=value["content_digest"],
            status=value["status"],
            ack=value["ack"],
            attempts=value["attempts"],
            created_at=_datetime(value["created_at"]),
        )

    return _Item(
        item_id=item["item_id"],
        cycle_id=item["cycle_id"],
        required_checkpoints=tuple(item["required_checkpoints"]),
        route_known=item["route_known"],
        context_revision=item["context_revision"],
        observations=observations,
        rework_operations=rework_operations,
        nonconformities=nonconformities,
        bases=bases,
        decisions=[decision(value) for value in item["decisions"]],
        dispositions=[decision(value) for value in item["dispositions"]],
        outbox={key: message(value) for key, value in item["outbox"].items()},
        next_result_version=item["next_result_version"],
        release_allowed_now=item["release_allowed_now"],
        blockers=tuple(item["blockers"]),
        review_required=item["review_required"],
        disposition_review_required=item.get("disposition_review_required", False),
        initial_cycle_id=item.get("initial_cycle_id", "cycle-1"),
        context=decode_context(item.get("context")),
        production_history=item.get("production_history", {}),
        cycles=item.get(
            "cycles",
            {
                item["cycle_id"]: {
                    "disposition_id": None,
                    "operation_run_ids": [],
                    "required_checkpoints": item["required_checkpoints"],
                    "control_sessions": {},
                    "checkpoint_operation_run_ids": {},
                }
            },
        ),
        evidence_record_ids=item.get("evidence_record_ids", []),
        scope_constraints=item.get("scope_constraints", {}),
        observation_bindings=item.get("observation_bindings", {}),
        control_replacements=item.get("control_replacements", {}),
    )


class PostgresQualityWorkflow:
    """Persist commands under the shared DP-01 demonstrator guard."""

    def __init__(self, engine: Engine, clock=None) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("The quality workflow requires PostgreSQL.")
        self.engine = engine
        self.clock = clock

    def execute_command(self, method, target_id, payload, *, actor_id, document):
        token = _command_document.set(document)
        try:
            return getattr(self, method)(target_id, actor_id=actor_id, **payload)
        finally:
            _command_document.reset(token)

    def open_item(self, item_id: str, *, connection=None, **kwargs) -> None:
        workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
        workflow.open_item(item_id, **kwargs)
        with self.engine.begin() if connection is None else nullcontext(connection) as connection:
            production_guard(connection)
            if connection.execute(
                select(quality_items).where(quality_items.c.item_id == item_id)
            ).first():
                raise ValueError("The item is already open.")
            now = workflow.clock()
            for constraint in connection.execute(
                select(quality_constraints).where(quality_constraints.c.is_open.is_(True))
            ).mappings():
                workflow._item(item_id).scope_constraints[constraint["constraint_id"]] = constraint[
                    "state"
                ]["reason"]
                workflow._item(item_id).evidence_record_ids.append(constraint["constraint_id"])
            self._prepare_bases(workflow, item_id)
            state = _compact(workflow._items[item_id])
            connection.execute(
                quality_items.insert().values(
                    item_id=item_id,
                    context_revision=0,
                    storage_revision=0,
                    state=state,
                    updated_at=now,
                )
            )
            connection.execute(
                quality_state_versions.insert().values(
                    item_id=item_id,
                    storage_revision=0,
                    context_revision=0,
                    snapshot=state,
                    created_at=now,
                )
            )
            self._persist_children(connection, workflow._items[item_id])

    @staticmethod
    def _prepare_bases(workflow, item_id):
        item = workflow._item(item_id)
        targets = {None, *item.nonconformities}
        existing = {
            b.target_id for b in item.bases.values() if b.context_revision == item.context_revision
        }
        for target in sorted(targets - existing, key=lambda x: x or ""):
            workflow.create_basis(item_id, target)

    def _hydrate(self, connection, state, *, basis_id=None, all_outbox=False):
        if state.get("storage_format") != 2:
            return _decode(state)
        value = copy.deepcopy(state)
        item_id = value["item_id"]
        refs = value["decision_refs"] + value["disposition_refs"]
        decisions = {
            row["decision_id"]: row["decision"]
            for row in connection.execute(
                select(quality_decisions).where(
                    quality_decisions.c.item_id == item_id,
                    quality_decisions.c.decision_id.in_(refs),
                )
            ).mappings()
        }
        if set(decisions) != set(refs):
            raise IntegrityViolation("An immutable decision is unavailable.")
        value["decisions"] = [decisions[key] for key in value["decision_refs"]]
        value["dispositions"] = [decisions[key] for key in value["disposition_refs"]]
        required = set(value["basis_refs"]) | {d["basis_id"] for d in decisions.values()}
        requested = required | ({basis_id} if basis_id else set())
        value["bases"] = {
            row["basis_id"]: row["snapshot"]
            for row in connection.execute(
                select(quality_bases).where(
                    quality_bases.c.item_id == item_id, quality_bases.c.basis_id.in_(requested)
                )
            ).mappings()
        }
        if not required.issubset(value["bases"]):
            raise IntegrityViolation("An immutable basis is unavailable.")
        query = (
            select(quality_outbox)
            .where(quality_outbox.c.item_id == item_id)
            .order_by(quality_outbox.c.result_version.desc())
        )
        if not all_outbox:
            query = query.limit(1)
        value["outbox"] = {
            row["message_id"]: dict(row) for row in connection.execute(query).mappings()
        }
        for msg in value["outbox"].values():
            msg["created_at"] = msg["created_at"].isoformat()
        return _decode(value)

    def read(self, item_id, operation, *, basis_id=None, all_outbox=False):
        """Consistent MVCC read; never acquires a writer/advisory lock or prepares a B."""
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin():
                row = (
                    connection.execute(
                        self._verified_query().where(quality_items.c.item_id == item_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise UnknownEntity("Unknown item.")
                self._verify_row(row)
                workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
                workflow._items[item_id] = self._hydrate(
                    connection, row["state"], basis_id=basis_id, all_outbox=all_outbox
                )
                return operation(workflow, connection)

    def _run(self, item_id: str, operation, *, connection=None, basis_id=None):
        with self.engine.begin() if connection is None else nullcontext(connection) as connection:
            production_guard(connection)
            row = (
                connection.execute(
                    select(quality_items)
                    .where(quality_items.c.item_id == item_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise UnknownEntity("Unknown item.")
            version = (
                connection.execute(
                    select(quality_state_versions).where(
                        quality_state_versions.c.item_id == item_id,
                        quality_state_versions.c.storage_revision == row["storage_revision"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if version is None or version["snapshot"] != row["state"]:
                raise IntegrityViolation(
                    "The mutable quality projection diverged from its version."
                )
            workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
            workflow._items[item_id] = self._hydrate(connection, row["state"], basis_id=basis_id)
            original = workflow._items[item_id]
            children = (
                set(original.bases),
                {d.decision_id for d in original.decisions + original.dispositions},
                set(original.outbox),
            )
            result = operation(workflow, connection)
            self._prepare_bases(workflow, item_id)
            item = workflow._items[item_id]
            now = workflow.clock()
            state = _compact(item)
            if state != row["state"]:
                storage_revision = row["storage_revision"] + 1
                connection.execute(
                    quality_items.update()
                    .where(quality_items.c.item_id == item_id)
                    .values(
                        context_revision=item.context_revision,
                        storage_revision=storage_revision,
                        state=state,
                        updated_at=now,
                    )
                )
                connection.execute(
                    quality_state_versions.insert().values(
                        item_id=item_id,
                        storage_revision=storage_revision,
                        context_revision=item.context_revision,
                        snapshot=state,
                        created_at=now,
                    )
                )
            self._persist_children(connection, item, children)
            return result

    def _persist_children(self, connection, item, existing=(set(), set(), set())) -> None:
        for basis in item.bases.values():
            if basis.basis_id in existing[0]:
                continue
            connection.execute(
                insert(quality_bases)
                .values(
                    basis_id=basis.basis_id,
                    item_id=basis.item_id,
                    context_revision=basis.context_revision,
                    target_id=basis.target_id,
                    snapshot=_encode(basis),
                    created_at=basis.created_at,
                )
                .on_conflict_do_nothing(index_elements=[quality_bases.c.basis_id])
            )
        for decision in [*item.decisions, *item.dispositions]:
            if decision.decision_id in existing[1]:
                continue
            connection.execute(
                insert(quality_decisions)
                .values(
                    decision_id=decision.decision_id,
                    item_id=item.item_id,
                    target_id=decision.target_id,
                    command_type=decision.command_type,
                    action=decision.action,
                    decision=_encode(decision),
                    created_at=decision.created_at,
                )
                .on_conflict_do_nothing(index_elements=[quality_decisions.c.decision_id])
            )
        for message in item.outbox.values():
            if message.message_id in existing[2]:
                continue
            if "confirmed_nonconformities" in message.payload:
                from app.adapters.shared_contract import validate

                validate("QualityResult", message.payload, external=True, response=True)
            connection.execute(
                insert(quality_outbox)
                .values(
                    message_id=message.message_id,
                    item_id=message.item_id,
                    result_version=message.result_version,
                    previous_message_id=message.previous_message_id,
                    context_revision=message.context_revision,
                    payload=message.payload,
                    content_digest=message.content_digest,
                    status=message.status,
                    ack=message.ack,
                    attempts=message.attempts,
                    created_at=message.created_at,
                )
                .on_conflict_do_update(
                    index_elements=[quality_outbox.c.message_id],
                    set_={
                        "status": message.status,
                        "ack": message.ack,
                        "attempts": message.attempts,
                    },
                )
            )
            connection.execute(
                insert(quality_deliveries)
                .values(
                    message_id=message.message_id,
                    status=message.status,
                    attempts=0,
                    next_attempt_at=message.created_at,
                )
                .on_conflict_do_nothing(index_elements=[quality_deliveries.c.message_id])
            )

    def _command(
        self,
        item_id: str,
        actor_id: str,
        command_type: str,
        target_id: str,
        key: str,
        payload: dict,
        operation,
    ):
        document = _command_document.get()
        digest = _digest(payload if document is None else document)

        def invoke(workflow, connection):
            saved = (
                connection.execute(
                    select(quality_commands).where(
                        quality_commands.c.actor_id == actor_id,
                        quality_commands.c.command_type == command_type,
                        quality_commands.c.target_id == target_id,
                        quality_commands.c.idempotency_key == key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if saved is not None:
                if saved["command_digest"] != digest:
                    raise IdempotencyConflict(
                        "The idempotency key was used with a different command body."
                    )
                return CommandResult(saved["result"], duplicate=True)
            result = operation(workflow)
            connection.execute(
                quality_commands.insert().values(
                    actor_id=actor_id,
                    command_type=command_type,
                    target_id=target_id,
                    idempotency_key=key,
                    command_digest=digest,
                    result=result,
                    created_at=workflow.clock(),
                )
            )
            return CommandResult(result, duplicate=False)

        return self._run(item_id, invoke, basis_id=payload.get("expected_basis_id"))

    def record_observation(self, item_id: str, **kwargs):
        return self._run(
            item_id, lambda workflow, connection: workflow.record_observation(item_id, **kwargs)
        )

    def record_rework_operation(self, item_id: str, **kwargs):
        return self._run(
            item_id,
            lambda workflow, connection: workflow.record_rework_operation(item_id, **kwargs),
        )

    def create_basis(self, item_id: str, target_id: str | None = None):
        return self._run(
            item_id, lambda workflow, connection: workflow.create_basis(item_id, target_id)
        )

    def decide_nonconformity(self, nonconformity_id: str, **kwargs):
        item_id = self._find_item_id(nonconformity_id)
        payload = {
            key: kwargs[key]
            for key in ("action", "reason", "expected_revision", "expected_basis_id")
        }
        payload.update(
            actor_id=kwargs["actor_id"],
            cause_status=kwargs.get("cause_status", "unresolved"),
            cause_category=kwargs.get("cause_category", "unknown"),
            cause_evidence_record_ids=tuple(kwargs.get("cause_evidence_record_ids", ())),
            operation_run_id=kwargs.get("operation_run_id"),
            operator_id=kwargs.get("operator_id"),
            action_record_id=kwargs.get("action_record_id"),
        )
        return self._command(
            item_id,
            kwargs["actor_id"],
            "nonconformity_decision",
            nonconformity_id,
            kwargs["idempotency_key"],
            payload,
            lambda workflow: workflow.decide_nonconformity(nonconformity_id, **kwargs),
        )

    def resolve_nonconformity(self, nonconformity_id: str, **kwargs):
        item_id = self._find_item_id(nonconformity_id)
        reference_field = (
            "control_record_ids" if "control_record_ids" in kwargs else "observation_ids"
        )
        payload = {
            key: kwargs[key]
            for key in (
                "reason",
                "expected_revision",
                "expected_basis_id",
                "rework_operation_id",
                "previous_operation_run_id",
                reference_field,
            )
        }
        payload["actor_id"] = kwargs["actor_id"]
        payload["action"] = "resolve"

        def resolve(workflow):
            arguments = dict(kwargs)
            if reference_field == "control_record_ids":
                records = tuple(arguments["control_record_ids"])
                item = workflow._item(item_id)
                observations = [
                    obs for obs in item.observations.values() if obs.record_id in records
                ]
                if not records or {obs.record_id for obs in observations} != set(records):
                    raise InvalidTransition("Control records must be inspections of this item.")
                arguments["observation_ids"] = [obs.observation_id for obs in observations]
            return workflow.resolve_nonconformity(nonconformity_id, **arguments)

        return self._command(
            item_id,
            kwargs["actor_id"],
            "resolution_decision",
            nonconformity_id,
            kwargs["idempotency_key"],
            payload,
            resolve,
        )

    def reopen_nonconformity(self, nonconformity_id, **kwargs):
        item_id = self._find_item_id(nonconformity_id)
        payload = {key: value for key, value in kwargs.items() if key != "idempotency_key"}
        payload["action"] = "reopen"
        return self._command(
            item_id,
            kwargs["actor_id"],
            "resolution_decision",
            nonconformity_id,
            kwargs["idempotency_key"],
            payload,
            lambda workflow: workflow.reopen_nonconformity(nonconformity_id, **kwargs),
        )

    def dispose(self, item_id: str, **kwargs):
        payload = {
            key: kwargs[key]
            for key in ("action", "reason", "expected_revision", "expected_basis_id")
        }
        payload["actor_id"] = kwargs["actor_id"]
        return self._command(
            item_id,
            kwargs["actor_id"],
            "item_disposition",
            item_id,
            kwargs["idempotency_key"],
            payload,
            lambda workflow: workflow.dispose(item_id, **kwargs),
        )

    def recover_control(self, item_id, **kwargs):
        return self._command(
            item_id,
            kwargs["actor_id"],
            "control_recovery",
            item_id,
            kwargs["idempotency_key"],
            {k: v for k, v in kwargs.items() if k != "idempotency_key"},
            lambda workflow: workflow.recover_control(item_id, **kwargs),
        )

    def start_cycle(self, item_id: str, **kwargs):
        if kwargs.get("required_checkpoints") is None:
            current = self.state(item_id)
            kwargs = {**kwargs, "required_checkpoints": current["required_checkpoints"]}
        payload = {
            key: kwargs[key]
            for key in (
                "cycle_id",
                "required_checkpoints",
                "expected_revision",
                "expected_basis_id",
                "reason",
            )
        }
        payload["actor_id"] = kwargs["actor_id"]
        payload["operation_run_ids"] = list(kwargs.get("operation_run_ids", ()))
        payload["control_sessions"] = kwargs.get("control_sessions", {})
        payload["checkpoint_operation_run_ids"] = kwargs.get("checkpoint_operation_run_ids", {})
        payload["rework_disposition_id"] = kwargs.get("rework_disposition_id")
        return self._command(
            item_id,
            kwargs["actor_id"],
            "control_cycle",
            item_id,
            kwargs["idempotency_key"],
            payload,
            lambda workflow: workflow.start_cycle(item_id, **kwargs),
        )

    def acknowledge(self, message_id: str, **kwargs):
        with self.engine.begin() as connection:
            return self.acknowledge_in(connection, message_id, **kwargs)

    def acknowledge_in(self, connection, message_id, **kwargs):
        from app.domain.quality import _Item

        row = (
            connection.execute(
                select(quality_outbox)
                .where(quality_outbox.c.message_id == message_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise UnknownEntity("Unknown outbox message.")
        workflow = QualityWorkflow()
        item = _Item(row["item_id"], "", ())
        item.outbox[message_id] = OutboxMessage(**dict(row))
        workflow._items[item.item_id] = item
        receipt = workflow.acknowledge(message_id, **kwargs)
        connection.execute(
            quality_outbox.update()
            .where(quality_outbox.c.message_id == message_id)
            .values(status="acknowledged", ack=receipt)
        )
        return receipt

    def state(self, item_id: str):
        return self.read(item_id, lambda workflow, _: workflow.state(item_id), all_outbox=True)

    def set_production_history(self, item_id: str, history: dict, record_ids):
        return self._run(
            item_id,
            lambda workflow, connection: workflow.set_production_history(
                item_id, history, record_ids
            ),
        )

    def get_basis(self, item_id: str, basis_id: str):
        with self.engine.connect() as connection:
            basis = connection.execute(
                select(quality_bases.c.snapshot).where(
                    quality_bases.c.item_id == item_id, quality_bases.c.basis_id == basis_id
                )
            ).scalar_one_or_none()
            if basis is None:
                raise UnknownEntity("Unknown basis.")
            return basis

    def analytics(self, item_ids=None, *, since=None, until=None):
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            query = self._verified_query()
            if item_ids is not None:
                query = query.where(quality_items.c.item_id.in_(tuple(item_ids)))
            rows = connection.execute(query).mappings()
            workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
            for row in rows:
                self._verify_row(row)
                item = self._hydrate(connection, row["state"])
                workflow._items[item.item_id] = item
            return workflow.analytics(since=since, until=until)

    def upgrade_storage(self):
        """Append a new current interpretation; preserve every legacy state and B."""
        with self.engine.begin() as connection:
            production_guard(connection)
            ids = list(connection.scalars(select(quality_items.c.item_id)))
            upgraded = 0
            for item_id in ids:

                def upgrade(workflow, _, item_id=item_id):
                    item = workflow._item(item_id)
                    if any(
                        b.context_revision == item.context_revision
                        and b.processor_version == "quality-v3"
                        for b in item.bases.values()
                    ):
                        return False
                    item.context_revision += 1
                    if item.dispositions:
                        item.disposition_review_required = True
                    workflow._emit(item)
                    return True

                upgraded += int(self._run(item_id, upgrade, connection=connection))
            return upgraded

    def list_states(self):
        with self.engine.connect() as connection:
            rows = connection.execute(self._verified_query()).mappings()
            states = []
            for row in rows:
                self._verify_row(row)
                workflow = QualityWorkflow()
                item = self._hydrate(connection, row["state"])
                workflow._items[item.item_id] = item
                states.append(workflow.state(item.item_id))
            return states

    def item_ids_page(self, cursor, limit):
        with self.engine.connect() as connection:
            return list(
                connection.scalars(
                    select(quality_items.c.item_id)
                    .where(quality_items.c.item_id > cursor)
                    .order_by(quality_items.c.item_id)
                    .limit(limit + 1)
                )
            )

    @staticmethod
    def _verified_query():
        return select(
            quality_items, quality_state_versions.c.snapshot.label("verified_snapshot")
        ).outerjoin(
            quality_state_versions,
            (
                (quality_state_versions.c.item_id == quality_items.c.item_id)
                & (quality_state_versions.c.storage_revision == quality_items.c.storage_revision)
            ),
        )

    @staticmethod
    def _verify_row(row):
        if row["verified_snapshot"] != row["state"]:
            raise IntegrityViolation("The mutable quality projection diverged from its version.")

    def _state_from_json(self, value):
        workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
        item = _decode(value)
        workflow._items[item.item_id] = item
        return workflow.state(item.item_id)

    def find_item_for_nonconformity(self, nonconformity_id):
        return self._find_item_id(nonconformity_id)

    def find_basis_item(self, basis_id):
        with self.engine.connect() as connection:
            row = connection.execute(
                select(quality_bases.c.item_id).where(quality_bases.c.basis_id == basis_id)
            ).first()
        if row is None:
            raise UnknownEntity("Unknown basis.")
        return row[0]

    def list_outbox(self):
        with self.engine.connect() as connection:
            rows = connection.execute(self._delivery_query()).mappings()
            return [dict(row) for row in rows]

    @staticmethod
    def _delivery_query():
        return select(
            *[column for column in quality_outbox.c if column.name not in {"status", "attempts"}],
            quality_deliveries.c.status,
            quality_deliveries.c.attempts,
            quality_deliveries.c.last_error,
            quality_deliveries.c.next_attempt_at,
            quality_deliveries.c.lease_until,
        ).join(quality_deliveries)

    def get_outbox(self, message_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    self._delivery_query().where(quality_outbox.c.message_id == message_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise UnknownEntity("Unknown outbox message.")
        return dict(row)

    def _find_item_id(self, nonconformity_id: str) -> str:
        with self.engine.connect() as connection:
            value = connection.scalar(
                select(quality_items.c.item_id).where(
                    quality_items.c.state["nonconformities"].has_key(nonconformity_id)
                )
            )
            if value is not None:
                return value
        raise UnknownEntity("Unknown nonconformity.")

    def _find_outbox_item(self, message_id: str) -> str:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(quality_outbox.c.item_id).where(quality_outbox.c.message_id == message_id)
            ).first()
        if row is None:
            raise UnknownEntity("Unknown outbox message.")
        return row[0]
