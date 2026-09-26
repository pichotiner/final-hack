"""PostgreSQL persistence for the executable DP-01 quality workflow."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from app.adapters.postgres import (
    quality_bases,
    quality_commands,
    quality_decisions,
    quality_items,
    quality_outbox,
)
from app.adapters.traceability import _lock
from app.domain.errors import IdempotencyConflict, UnknownEntity
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


def _decode(item: dict):
    from app.domain.quality import _Item

    observations = {
        key: Observation(**{**value, "created_at": _datetime(value["created_at"])})
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
    )


class PostgresQualityWorkflow:
    """Persist ``QualityWorkflow`` commands under the per-item SQL guard."""

    def __init__(self, engine: Engine, clock=None) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("The quality workflow requires PostgreSQL.")
        self.engine = engine
        self.clock = clock

    def open_item(self, item_id: str, **kwargs) -> None:
        workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
        workflow.open_item(item_id, **kwargs)
        with self.engine.begin() as connection:
            if connection.execute(
                select(quality_items).where(quality_items.c.item_id == item_id)
            ).first():
                raise ValueError("The item is already open.")
            now = workflow.clock()
            connection.execute(
                quality_items.insert().values(
                    item_id=item_id,
                    context_revision=0,
                    state=_encode(workflow._items[item_id]),
                    updated_at=now,
                )
            )

    def _run(self, item_id: str, operation):
        with self.engine.begin() as connection:
            _lock(connection, "quality-item", item_id)
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
            workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
            workflow._items[item_id] = _decode(row["state"])
            result = operation(workflow, connection)
            item = workflow._items[item_id]
            now = workflow.clock()
            connection.execute(
                quality_items.update()
                .where(quality_items.c.item_id == item_id)
                .values(context_revision=item.context_revision, state=_encode(item), updated_at=now)
            )
            self._persist_children(connection, item)
            return result

    def _persist_children(self, connection, item) -> None:
        for basis in item.bases.values():
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
                    set_={"status": message.status, "ack": message.ack},
                )
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
        digest = _digest(payload)

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
                return saved["result"]
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
            return result

        return self._run(item_id, invoke)

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
        payload["actor_id"] = kwargs["actor_id"]
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
        payload = {
            key: kwargs[key]
            for key in (
                "reason",
                "expected_revision",
                "expected_basis_id",
                "rework_operation_id",
                "previous_operation_run_id",
                "observation_ids",
            )
        }
        payload["actor_id"] = kwargs["actor_id"]
        return self._command(
            item_id,
            kwargs["actor_id"],
            "resolution_decision",
            nonconformity_id,
            kwargs["idempotency_key"],
            payload,
            lambda workflow: workflow.resolve_nonconformity(nonconformity_id, **kwargs),
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
        item_id = self._find_outbox_item(message_id)
        return self._run(
            item_id, lambda workflow, connection: workflow.acknowledge(message_id, **kwargs)
        )

    def state(self, item_id: str):
        return self._run(item_id, lambda workflow, connection: workflow.state(item_id))

    def analytics(self, item_ids=None):
        with self.engine.connect() as connection:
            query = select(quality_items)
            if item_ids is not None:
                query = query.where(quality_items.c.item_id.in_(tuple(item_ids)))
            rows = connection.execute(query).mappings()
            workflow = QualityWorkflow(clock=self.clock) if self.clock else QualityWorkflow()
            for row in rows:
                item = _decode(row["state"])
                workflow._items[item.item_id] = item
            return workflow.analytics()

    def _find_item_id(self, nonconformity_id: str) -> str:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(quality_items.c.item_id, quality_items.c.state)
            ).mappings()
            for row in rows:
                if nonconformity_id in row["state"].get("nonconformities", {}):
                    return row["item_id"]
        raise UnknownEntity("Unknown nonconformity.")

    def _find_outbox_item(self, message_id: str) -> str:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(quality_outbox.c.item_id).where(quality_outbox.c.message_id == message_id)
            ).first()
        if row is None:
            raise UnknownEntity("Unknown outbox message.")
        return row[0]
