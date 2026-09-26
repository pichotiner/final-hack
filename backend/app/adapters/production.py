"""DP-01 transaction: authenticated raw, traceability, quality, basis and outbox."""

import json
from contextlib import nullcontext
from dataclasses import asdict, replace
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select

from app.adapters.locks import production_guard
from app.adapters.postgres import (
    _record,
    event_projections,
    quality_commands,
    quality_constraints,
    quality_context_versions,
    quality_erp_imports,
    quality_external_streams,
    quality_items,
    raw_events,
)
from app.adapters.quality import PostgresQualityWorkflow
from app.adapters.traceability import PostgresTraceabilityJournal, _json_value
from app.application.source_validation import validate_source_payload
from app.domain.errors import IdempotencyConflict, InvalidTransition, StaleRevision, UnknownEntity
from app.domain.events import StoredEvent
from app.domain.quality import _digest
from app.domain.traceability import rebuild_history


class PostgresProductionJournal(PostgresTraceabilityJournal):
    def __init__(self, engine, protector):
        super().__init__(engine, protector)
        self.quality = PostgresQualityWorkflow(engine)

    def import_context(self, item_id, context, *, import_id, actor_id, connection=None):
        """Import an already mapped canonical context; external parsing is an adapter concern."""
        digest = _digest({"item_id": item_id, "context": asdict(context), "actor_id": actor_id})
        with self.engine.begin() if connection is None else nullcontext(connection) as connection:
            production_guard(connection)
            saved = (
                connection.execute(
                    select(quality_context_versions).where(
                        quality_context_versions.c.import_id == import_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if saved:
                if saved["content_digest"] != digest:
                    raise IdempotencyConflict("Context import ID has different content.")
                return {"status": "duplicate", "import_id": import_id}
            stream = {
                "producer_id": context.producer_id,
                "external_system": context.external_system,
                "external_order_id": context.external_order_id,
                "external_item_id": context.external_item_id,
            }
            mapping = (
                connection.execute(
                    select(quality_external_streams).where(
                        quality_external_streams.c.item_id == item_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if mapping is not None and any(mapping[key] != value for key, value in stream.items()):
                raise InvalidTransition("An item's external result stream cannot be changed.")
            owner = connection.scalar(
                select(quality_external_streams.c.item_id).where(
                    *(
                        getattr(quality_external_streams.c, key) == value
                        for key, value in stream.items()
                    )
                )
            )
            if owner is not None and owner != item_id:
                raise InvalidTransition(
                    "The external result stream already belongs to another item."
                )
            if mapping is None:
                connection.execute(
                    quality_external_streams.insert().values(item_id=item_id, **stream)
                )
            exists = connection.scalar(
                select(quality_items.c.item_id).where(quality_items.c.item_id == item_id)
            )
            changed = True
            if exists is None:
                self.quality.open_item(item_id, context=context, connection=connection)
            else:

                def update(workflow, connection):
                    item = workflow._item(item_id)
                    if item.context == context:
                        return False
                    if item.cycle_id != item.initial_cycle_id:
                        raise InvalidTransition("Changing a rework control plan requires review.")
                    item.context = context
                    item.route_known = True
                    item.required_checkpoints = tuple(
                        rule.checkpoint_id for rule in context.controls
                    )
                    item.cycles[item.initial_cycle_id].update(
                        operation_run_ids=list(context.required_operations),
                        required_checkpoints=item.required_checkpoints,
                        control_sessions={
                            rule.checkpoint_id: rule.control_session_id for rule in context.controls
                        },
                        checkpoint_operation_run_ids={
                            rule.checkpoint_id: rule.operation_run_id for rule in context.controls
                        },
                    )
                    if item.dispositions:
                        item.disposition_review_required = True
                    for nc in item.nonconformities.values():
                        if nc.decision_ids:
                            nc.review_required = True
                    item.review_required = any(
                        nc.review_required for nc in item.nonconformities.values()
                    )
                    return True

                changed = self.quality._run(item_id, update, connection=connection)
            if changed:

                def accept(workflow, connection):
                    item = workflow._item(item_id)
                    item.context_revision += 1
                    workflow._emit(item)
                    workflow.create_basis(item_id)

                self.quality._run(item_id, accept, connection=connection)
                self._backfill(connection, item_id)
            connection.execute(
                quality_context_versions.insert().values(
                    import_id=import_id,
                    item_id=item_id,
                    content_digest=digest,
                    snapshot=asdict(context),
                    actor_id=actor_id,
                    created_at=self._now(),
                )
            )
            return {"status": "accepted", "import_id": import_id}

    def import_work_order(self, order, mapper, *, actor_id):
        """One complete ERP order, all mapped items and receipt under the P3 writer guard."""
        mapper.contract.validate("WorkOrder", order)
        profile = mapper.profile
        system = profile["external_system"]
        digest, profile_digest = _digest(order), _digest(profile)
        with self.engine.begin() as connection:
            production_guard(connection)
            saved = (
                connection.execute(
                    select(quality_erp_imports).where(
                        quality_erp_imports.c.external_system == system,
                        quality_erp_imports.c.message_id == order["message_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if saved:
                if saved["content_digest"] != digest or saved["profile_digest"] != profile_digest:
                    raise IdempotencyConflict(
                        "ERP message ID already has different content or mapping."
                    )
                return {**saved["receipt"], "status": "duplicate"}
            pinned_digest = connection.scalar(
                select(quality_erp_imports.c.profile_digest)
                .where(
                    quality_erp_imports.c.external_system == system,
                    quality_erp_imports.c.profile_id == profile["profile_id"],
                    quality_erp_imports.c.profile_version == profile["profile_version"],
                )
                .limit(1)
            )
            if pinned_digest is not None and pinned_digest != profile_digest:
                raise IdempotencyConflict("A published mapping profile version cannot change.")
            latest = connection.scalar(
                select(quality_erp_imports.c.revision)
                .where(
                    quality_erp_imports.c.external_system == system,
                    quality_erp_imports.c.external_order_id == order["external_order_id"],
                )
                .order_by(quality_erp_imports.c.revision.desc())
                .limit(1)
            )
            if latest is not None and order["revision"] <= latest:
                raise InvalidTransition("ERP order revision must advance with a new message ID.")
            contexts = mapper.map(order)
            for item_id, context in contexts.items():
                import_id = str(
                    uuid5(NAMESPACE_URL, repr(("erp-import", system, order["message_id"], item_id)))
                )
                self.import_context(
                    item_id, context, import_id=import_id, actor_id=actor_id, connection=connection
                )
            now = self._now()
            receipt = {
                "status": "accepted",
                "external_system": system,
                "message_id": order["message_id"],
                "external_order_id": order["external_order_id"],
                "revision": order["revision"],
                "item_ids": list(contexts),
                "actor_id": actor_id,
                "received_at": now.isoformat(),
                "content_digest": digest,
                "profile_digest": profile_digest,
            }
            connection.execute(
                quality_erp_imports.insert().values(
                    external_system=system,
                    message_id=order["message_id"],
                    external_order_id=order["external_order_id"],
                    revision=order["revision"],
                    content_digest=digest,
                    profile_id=profile["profile_id"],
                    profile_version=profile["profile_version"],
                    profile_digest=profile_digest,
                    document=order,
                    profile=profile,
                    receipt=receipt,
                    created_at=now,
                )
            )
            return receipt

    def _backfill(self, connection, item_id):
        projected = set(
            connection.scalar(
                select(quality_items.c.state).where(quality_items.c.item_id == item_id)
            ).get("evidence_record_ids", ())
        )
        rows = list(
            connection.execute(
                select(raw_events)
                .where(
                    raw_events.c.item_id == item_id,
                    raw_events.c.identification_status == "reliable",
                )
                .order_by(raw_events.c.received_at, raw_events.c.source_id, raw_events.c.event_id)
            ).mappings()
        )
        facts = []
        final_history = None
        for row in rows:
            stored = _record(row)
            raw, fact = self._fact(stored)
            facts.append(fact)
            record_id = str(stored.metadata.record_id)
            if record_id in projected:
                continue
            document = json.loads(raw)
            validate_source_payload(document)
            final_history = rebuild_history(item_id, facts)
            snapshot = json.loads(json.dumps(asdict(final_history), default=_json_value))

            def project(
                workflow,
                connection,
                document=document,
                record_id=record_id,
                snapshot=snapshot,
                occurred_at=stored.metadata.envelope.occurred_at,
            ):
                status, issues = workflow.project_source_fact(
                    item_id,
                    document,
                    record_id,
                    snapshot,
                    occurred_at,
                )
                workflow.create_basis(item_id)
                return status, issues

            status, issues = self.quality._run(item_id, project, connection=connection)
            connection.execute(
                event_projections.update()
                .where(event_projections.c.record_id == stored.metadata.record_id)
                .values(status=status, issues=list(issues), revision=2, updated_at=self._now())
            )
        if final_history is not None:
            self._save_with_production_states(connection, final_history)

    @staticmethod
    def _now():
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def _open_constraint(self, connection, constraint_id, reason):
        connection.execute(
            quality_constraints.insert().values(
                constraint_id=constraint_id,
                is_open=True,
                state={"reason": reason, "opened_at": self._now().isoformat(), "resolution": None},
            )
        )
        ids = connection.scalars(select(quality_items.c.item_id)).all()
        for item_id in ids:

            def constrain(workflow, connection, item_id=item_id):
                item = workflow._item(item_id)
                item.scope_constraints[constraint_id] = reason
                if constraint_id not in item.evidence_record_ids:
                    item.evidence_record_ids.append(constraint_id)
                item.context_revision += 1
                if item.dispositions:
                    item.disposition_review_required = True
                workflow._emit(item)
                workflow.create_basis(item_id)

            self.quality._run(item_id, constrain, connection=connection)

    def append_if_absent(self, event):
        envelope = event.metadata.envelope
        with self.engine.begin() as connection:
            production_guard(connection)
            saved = (
                connection.execute(
                    select(raw_events).where(
                        raw_events.c.source_id == envelope.source_id,
                        raw_events.c.event_id == envelope.event_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if saved is not None:
                return _record(saved), False
            raw, candidate = self._fact(event)
            document = json.loads(raw)
            validate_source_payload(document)
            if envelope.item_id is None or envelope.identification_status != "reliable":
                status, issues = "unresolved", ("unreliable_item_identification",)
                self._open_constraint(connection, str(event.metadata.record_id), issues[0])
                history = None
            else:
                item_id = envelope.item_id
                managed = connection.scalar(
                    select(quality_items.c.item_id).where(quality_items.c.item_id == item_id)
                )
                history = rebuild_history(item_id, [*self._facts(connection, item_id), candidate])
                snapshot = json.loads(json.dumps(asdict(history), default=_json_value))
                if managed is None:
                    status, issues = "unresolved", ("work_order_not_resolved",)
                else:

                    def project(workflow, connection):
                        result = workflow.project_source_fact(
                            item_id,
                            document,
                            str(event.metadata.record_id),
                            snapshot,
                            envelope.occurred_at,
                        )
                        workflow.create_basis(item_id)
                        return result

                    status, issues = self.quality._run(item_id, project, connection=connection)
            meta = replace(event.metadata, initial_projection_status=status, initial_issues=issues)
            stored, inserted = self._append(
                connection, StoredEvent(meta, self.protector.seal(raw, meta))
            )
            if history is not None:
                self._save_with_production_states(connection, history)
            return stored, inserted

    def _save_with_production_states(self, connection, history):
        states = {
            row["record_id"]: row
            for row in connection.execute(
                select(event_projections).where(
                    event_projections.c.record_id.in_(
                        [entry.record_id for entry in history.timeline]
                    )
                )
            ).mappings()
        }
        history = replace(
            history,
            timeline=tuple(
                replace(
                    entry,
                    projection_status=states[entry.record_id]["status"],
                    issues=tuple(states[entry.record_id]["issues"]),
                )
                for entry in history.timeline
            ),
        )
        return self._save(connection, history)

    def resolve_scope(self, constraint_id, *, actor_id, reason, idempotency_key, bases):
        """Close exactly one pilot-wide constraint against pinned bases for every current item."""
        if not reason.strip() or not actor_id or not idempotency_key:
            raise InvalidTransition("Scope resolution requires author, reason and command key.")
        digest = _digest({"reason": reason, "bases": bases})
        with self.engine.begin() as connection:
            production_guard(connection)
            key = (
                quality_commands.c.actor_id == actor_id,
                quality_commands.c.command_type == "scope_resolution",
                quality_commands.c.target_id == constraint_id,
                quality_commands.c.idempotency_key == idempotency_key,
            )
            saved = (
                connection.execute(select(quality_commands).where(*key)).mappings().one_or_none()
            )
            if saved:
                if saved["command_digest"] != digest:
                    raise IdempotencyConflict("Scope command key has different content.")
                return saved["result"]
            constraint = (
                connection.execute(
                    select(quality_constraints).where(
                        quality_constraints.c.constraint_id == constraint_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if constraint is None:
                raise UnknownEntity("Unknown scope constraint.")
            if not constraint["is_open"]:
                raise InvalidTransition("This constraint is already resolved.")
            ids = connection.scalars(select(quality_items.c.item_id)).all()
            if set(ids) != set(bases):
                raise StaleRevision("The pilot item scope changed; review all current items.")
            for item_id in ids:

                def resolve(workflow, connection, item_id=item_id):
                    item = workflow._item(item_id)
                    expected = bases[item_id]
                    workflow._check_context(
                        item, expected["context_revision"], expected["basis_id"]
                    )
                    item.scope_constraints.pop(constraint_id, None)
                    item.context_revision += 1
                    workflow._emit(item)

                self.quality._run(item_id, resolve, connection=connection)
            result = {
                "constraint_id": constraint_id,
                "decision_id": str(uuid4()),
                "actor_id": actor_id,
                "reason": reason,
                "bases": bases,
                "decided_at": self._now().isoformat(),
            }
            connection.execute(
                quality_constraints.update()
                .where(quality_constraints.c.constraint_id == constraint_id)
                .values(is_open=False, state={**constraint["state"], "resolution": result})
            )
            connection.execute(
                quality_commands.insert().values(
                    actor_id=actor_id,
                    command_type="scope_resolution",
                    target_id=constraint_id,
                    idempotency_key=idempotency_key,
                    command_digest=digest,
                    result=result,
                    created_at=self._now(),
                )
            )
            return result

    def replay(self, item_id):
        # Quality human decisions must not be replayed as source events.
        # Verify the complete selected raw set and repair only the traceability read model.
        with self.engine.begin() as connection:
            production_guard(connection)
            facts = self._facts(connection, item_id)
            if not facts:
                return None
            history = rebuild_history(item_id, facts)
            return self._save_with_production_states(connection, history)
