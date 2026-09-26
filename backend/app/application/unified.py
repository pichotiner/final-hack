"""One transaction joining the protected ledger, graph, bases, decisions and wire outbox.

The prototype is a small single-pilot coordinator. External sources do not acquire
human decision authority, graph reachability is not a cause, and completeness is
an explicit owner assertion. Production hypothesis/edge work is planned separately.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

import rfc8785
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.adapters.json_events import JsonEventCodec
from app.adapters.postgres import (
    PostgresEventJournal,
    _record,
    quality_items,
    raw_events,
    unified_state,
)
from app.adapters.quality import PostgresQualityWorkflow, _decode, _encode
from app.adapters.traceability import PostgresTraceabilityJournal, _lock
from app.domain.errors import (
    AckMismatch,
    BasisMismatch,
    EventConflict,
    IdempotencyConflict,
    UnknownEntity,
)
from app.domain.events import RecordMetadata, StoredEvent
from app.domain.evidence_graph import Change, Completeness, Membership, Snapshot, assess
from app.domain.quality import QualityWorkflow
from app.domain.traceability import rebuild_history


def now():
    return datetime.now(UTC)


def digest(body):
    return hashlib.sha256(rfc8785.dumps(body)).hexdigest()


class UnifiedChain:
    """Persistent PostgreSQL chain. Every writer uses this same global pilot guard."""

    def __init__(self, engine, protector, *, namespace="efqa-demo"):
        self.engine, self.protector, self.namespace = engine, protector, namespace
        self.codec = JsonEventCodec()
        self.journal = PostgresEventJournal(engine)
        self.history = PostgresTraceabilityJournal(engine, protector)
        self.persistence = PostgresQualityWorkflow(engine)

    def execute(self, operation):
        with self.engine.begin() as connection:
            _lock(connection, "unified-pilot", self.namespace)
            row = (
                connection.execute(
                    select(unified_state).where(unified_state.c.namespace == self.namespace)
                )
                .mappings()
                .one_or_none()
            )
            state = (
                copy.deepcopy(row["state"])
                if row
                else {
                    "items": {},
                    "orders": {},
                    "composition": None,
                    "refs": {},
                    "relations": {},
                    "commands": {},
                    "wire": {},
                    "cycles": {},
                    "scope_constraints": [],
                }
            )
            workflow = QualityWorkflow()
            workflow._items = {key: _decode(value) for key, value in state["items"].items()}
            result = operation(state, workflow, connection)
            self._wire(state, workflow)
            state["items"] = {key: _encode(item) for key, item in workflow._items.items()}
            for item in workflow._items.values():
                connection.execute(
                    insert(quality_items)
                    .values(
                        item_id=item.item_id,
                        context_revision=item.context_revision,
                        state=_encode(item),
                        updated_at=now(),
                    )
                    .on_conflict_do_update(
                        index_elements=[quality_items.c.item_id],
                        set_={
                            "context_revision": item.context_revision,
                            "state": _encode(item),
                            "updated_at": now(),
                        },
                    )
                )
                self.persistence._persist_children(connection, item)
            connection.execute(
                insert(unified_state)
                .values(namespace=self.namespace, state=state)
                .on_conflict_do_update(
                    index_elements=[unified_state.c.namespace], set_={"state": state}
                )
            )
            return copy.deepcopy(result)

    def import_order(self, order):
        from contracts.validation import validate

        validate("WorkOrder", order, external=True)

        def apply(state, workflow, _):
            key = order["external_order_id"]
            if key in state["orders"]:
                if state["orders"][key] == order:
                    return {"status": "duplicate", "id": key}
                raise IdempotencyConflict(
                    "Order revision changes need explicit applicability review."
                )
            state["orders"][key] = copy.deepcopy(order)
            for item_id in order["item_ids"]:
                if item_id in workflow._items:
                    raise IdempotencyConflict("An item is already assigned to a different order.")
                workflow.open_item(
                    item_id, required_checkpoints=[x["checkpoint_id"] for x in order["checkpoints"]]
                )
                state["cycles"][item_id] = {"cycle-1": {"runs": [], "disposition_id": None}}
                state["relations"][item_id] = []
                for record_id in state.get("scope_constraints", []):
                    state["relations"][item_id].append(
                        {
                            "record_id": record_id,
                            "impact": {
                                "item_id": item_id,
                                "classification": "possibly_affected",
                                "reason": "open_scope_applies_to_future_item",
                                "path": [record_id, "scope_unknown", item_id],
                            },
                            "coverage": "open",
                            "issues": ["open_scope_applies_to_future_item"],
                        }
                    )
            return {"status": "accepted", "id": key}

        return self.execute(apply)

    def set_composition(self, document):
        """Explicit owner fixture/statement, not inferred from CAD or an item_received list."""
        snapshot = Snapshot(
            document["snapshot_id"],
            tuple(document["items"]),
            tuple(Membership(**entry) for entry in document["memberships"]),
            Completeness(document["proof_id"], document["snapshot_id"])
            if document.get("proof_id")
            else None,
        )

        def apply(state, workflow, _):
            if state["composition"] == document:
                return {"status": "duplicate"}
            if not set(workflow._items) <= set(snapshot.items):
                raise ValueError("Composition catalog omits known assemblies.")
            # Exercise the same cycle/conflict checks as ingestion; current conflicts remain
            # a stored unknown assertion, not a falsely accepted physical installation.
            if not snapshot.items:
                raise ValueError("Composition catalog cannot be empty.")
            report = assess(
                snapshot, Change(document["snapshot_id"], reliable_items=(snapshot.items[0],))
            )
            state["composition"] = copy.deepcopy(document)
            state["composition_issues"] = list(report.issues)
            for ref in state["refs"].values():
                seed = (
                    ref["item_id"]
                    if ref["item_id"] in snapshot.items
                    and ref["identification_status"] == "reliable"
                    else None
                )
                affected = assess(
                    snapshot,
                    Change(
                        ref["record_id"],
                        reliable_items=(seed,) if seed else (),
                        unknown_scope=seed is None,
                    ),
                )
                for entry in affected.explanations:
                    if (
                        entry.item_id not in workflow._items
                        or entry.classification == "excluded_under_assumptions"
                    ):
                        continue
                    if any(
                        link["record_id"] == ref["record_id"]
                        for link in state["relations"][entry.item_id]
                    ):
                        continue
                    state["relations"][entry.item_id].append(
                        {
                            "record_id": ref["record_id"],
                            "impact": asdict(entry),
                            "coverage": affected.coverage,
                            "issues": list(affected.issues),
                        }
                    )
            for item in workflow._items.values():
                item.context_revision += 1
                self._context(state, workflow, item.item_id)
                if item.decisions or item.dispositions:
                    self._review(item)
                workflow._emit(item)
            return {"status": "accepted", "issues": list(report.issues)}

        return self.execute(apply)

    def _snapshot(self, state, workflow):
        document = state["composition"]
        if document:
            return Snapshot(
                document["snapshot_id"],
                tuple(document["items"]),
                tuple(Membership(**entry) for entry in document["memberships"]),
                Completeness(document["proof_id"], document["snapshot_id"])
                if document.get("proof_id")
                else None,
            )
        return Snapshot("composition-unknown", tuple(workflow._items), ())

    @staticmethod
    def _review(item):
        for card in item.nonconformities.values():
            if card.decision_ids:
                card.review_required = True
        item.review_required = any(card.review_required for card in item.nonconformities.values())
        if item.dispositions:
            item.disposition_review_required = True

    def ingest(self, raw, source_id):
        from contracts.validation import validate_source_event

        prepared = self.codec.decode(raw)
        document = json.loads(raw)
        validate_source_event(document)
        if document["source_id"] != source_id:
            from app.domain.errors import SourceMismatch

            raise SourceMismatch("Authenticated source does not match envelope.")

        def apply(state, workflow, connection):
            existing = (
                connection.execute(
                    select(raw_events).where(
                        raw_events.c.source_id == source_id,
                        raw_events.c.event_id == document["event_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                saved = _record(existing)
                original = self.codec.decode(self.protector.open(saved.protection, saved.metadata))
                if original.content_digest != prepared.content_digest:
                    raise EventConflict("Same source/event ID, different body.")
                return self._receipt(saved, "duplicate")
            record_id, received = str(uuid4()), now()
            payload = document["payload"]
            requested_item = document["item_id"]
            snapshot = self._snapshot(state, workflow)
            reliable = document.get("identification_status", "uncertain") == "reliable"
            seed = requested_item if requested_item in snapshot.items and reliable else None
            report = assess(
                snapshot,
                Change(
                    record_id, reliable_items=(seed,) if seed else (), unknown_scope=seed is None
                ),
            )
            affected = [
                entry
                for entry in report.explanations
                if entry.item_id in workflow._items
                and entry.classification != "excluded_under_assumptions"
            ]
            known = requested_item in workflow._items and reliable
            cycle = payload.get("control_cycle_id")
            if known and not cycle:
                run = document.get("operation_run_id")
                matches = (
                    [
                        cid
                        for cid, details in state["cycles"][requested_item].items()
                        if run in details["runs"]
                    ]
                    if run
                    else []
                )
                cycle = matches[0] if len(matches) == 1 else None
            issues = list(report.issues)
            if document.get("time_quality", "uncertain") != "synchronized":
                issues.append("uncertain_source_time")
            if seed is None:
                issues.append("item_or_assembly_link_unresolved")
            if document["event_type"] == "inspection_observed" and cycle not in state["cycles"].get(
                requested_item, {}
            ):
                issues.append("control_cycle_unresolved")
            meta = RecordMetadata(
                UUID(record_id),
                prepared.envelope,
                received,
                prepared.content_digest,
                "unresolved" if issues else "applied",
                tuple(issues),
            )
            stored = StoredEvent(meta, self.protector.seal(raw, meta))
            self.journal._append(connection, stored)
            state["refs"][record_id] = {
                "record_id": record_id,
                "source_id": source_id,
                "event_id": document["event_id"],
                "content_digest": prepared.content_digest,
                "event_type": document["event_type"],
                "occurred_at": prepared.envelope.occurred_at.isoformat(),
                "received_at": received.isoformat(),
                "projection_status": meta.initial_projection_status,
                "control_cycle_id": cycle,
                "analyzer_version": payload.get("analyzer_version"),
                "issues": issues,
                "item_id": requested_item,
                "identification_status": document.get("identification_status", "uncertain"),
                "line_id": document.get("line_id"),
                "operation_run_id": document.get("operation_run_id"),
            }
            if report.coverage == "open":
                state.setdefault("scope_constraints", []).append(record_id)
            for entry in affected:
                item = workflow._items[entry.item_id]
                state["relations"][entry.item_id].append(
                    {
                        "record_id": record_id,
                        "impact": asdict(entry),
                        "coverage": report.coverage,
                        "issues": list(report.issues),
                    }
                )
                prior_revision = item.context_revision
                old_message_ids = set(item.outbox)
                next_version = item.next_result_version
                is_rework = payload.get("operation_type_id") == "rework"
                if entry.item_id == requested_item and known:
                    run = document.get("operation_run_id")
                    if document["event_type"] == "operation_started" and run and not is_rework:
                        runs = state["cycles"][entry.item_id]["cycle-1"]["runs"]
                        if run not in runs:
                            runs.append(run)
                    if (
                        document["event_type"] == "inspection_observed"
                        and cycle in state["cycles"][entry.item_id]
                    ):
                        signs = payload["defects"]
                        for index, sign in enumerate(signs or [None]):
                            workflow.record_observation(
                                entry.item_id,
                                observation_id=f"{record_id}:{index}",
                                checkpoint_id=payload["checkpoint_id"],
                                result=payload["inspection_result"],
                                quality=payload["observation_quality"],
                                source_event_id=document["event_id"],
                                source_id=source_id,
                                component_id=sign.get("component_id") if sign else None,
                                region_id=sign.get("region_id") if sign else None,
                                defect_ref=sign.get("defect_ref") if sign else None,
                                defect_type=sign.get("type") if sign else None,
                                analyzer_version=payload.get("analyzer_version"),
                                record_id=record_id,
                                cycle_id=cycle,
                            )
                    elif document["event_type"] == "operation_finished" and is_rework:
                        active = state["cycles"][entry.item_id][item.cycle_id]
                        if run not in active["runs"]:
                            raise ValueError("Rework run was not assigned to this control cycle.")
                        workflow.record_rework_operation(
                            entry.item_id,
                            operation_id=run,
                            previous_operation_run_id=payload["previous_operation_run_id"],
                            cycle_id=item.cycle_id,
                        )
                # Reference methods may emit once per defect element. None of those
                # intermediate states is a source transaction; publish exactly one final
                # revision/snapshot per affected item for this accepted raw message.
                item.context_revision = prior_revision + 1
                item.outbox = {
                    key: value for key, value in item.outbox.items() if key in old_message_ids
                }
                item.next_result_version = next_version
                if item.decisions or item.dispositions:
                    last = [*item.decisions, *item.dispositions]
                    # New cycle observations are expected repair evidence. A new fact in the
                    # decision's cycle, unknown scope or an older cycle after release needs review.
                    if (
                        report.coverage != "complete"
                        or not cycle
                        or any(d.control_cycle_id == cycle for d in last)
                    ):
                        self._review(item)
                self._context(state, workflow, entry.item_id)
                workflow._emit(item)
            if known:
                self.history._save(
                    connection,
                    rebuild_history(
                        requested_item, self.history._facts(connection, requested_item)
                    ),
                )
            return self._receipt(stored, "accepted")

        return self.execute(apply)

    @staticmethod
    def _receipt(stored, status):
        meta = stored.metadata
        return {
            "record_id": str(meta.record_id),
            "source_id": meta.envelope.source_id,
            "event_id": meta.envelope.event_id,
            "status": status,
            "received_at": meta.received_at.isoformat(),
            "projection_status": meta.initial_projection_status,
            "issues": list(meta.initial_issues),
        }

    def _context(self, state, workflow, item_id):
        item = workflow._items[item_id]
        order = next(value for value in state["orders"].values() if item_id in value["item_ids"])
        inputs, unknowns, impact = [], [], []
        for relation in state["relations"][item_id]:
            ref = state["refs"][relation["record_id"]]
            excluded = (
                ref["control_cycle_id"] is not None and ref["control_cycle_id"] != item.cycle_id
            )
            inputs.append(
                {
                    **ref,
                    "role": "excluded" if excluded else "used",
                    "reason": "different_control_cycle; retained for historical review"
                    if excluded
                    else None,
                }
            )
            unknowns.extend(ref["issues"])
            impact.append(relation["impact"])
        contradictions, completeness = [], []
        for checkpoint in item.required_checkpoints:
            obs = [
                entry
                for entry in item.observations.values()
                if entry.cycle_id == item.cycle_id and entry.checkpoint_id == checkpoint
            ]
            results = {entry.effective_result for entry in obs}
            disputed = "defect_signs" in results and "no_defect_signs" in results
            status = (
                "missing"
                if not obs
                else "disputed"
                if disputed
                else "not_assessable"
                if "not_assessable" in results
                else "assessable"
            )
            ids = list(dict.fromkeys(entry.record_id for entry in obs if entry.record_id))
            if disputed:
                contradictions.append(
                    {
                        "checkpoint_id": checkpoint,
                        "record_ids": ids,
                        "reason": "conflicting_inspection_results",
                    }
                )
                for ref in inputs:
                    if ref["record_id"] in ids:
                        ref.update(role="contradictory", reason="conflicting_inspection_results")
            completeness.append(
                {
                    "checkpoint_id": checkpoint,
                    "requirement_version": order["control_plan_version"],
                    "control_cycle_id": item.cycle_id,
                    "assessment_status": status,
                    "record_ids": ids,
                    "issues": [] if status == "assessable" else [status],
                }
            )
        composition = state["composition"]
        blockers = []
        if not composition or not composition.get("proof_id"):
            blockers.append("composition_completeness_unknown")
        if composition and item_id not in composition["items"]:
            blockers.append("composition_snapshot_does_not_cover_item")
        blockers.extend(state.get("composition_issues", []))
        if any(relation["coverage"] == "open" for relation in state["relations"][item_id]):
            blockers.append("scope_unknown")
        method = {value["checkpoint_id"]: value["method_version"] for value in order["checkpoints"]}
        if any(
            obs.cycle_id == item.cycle_id and obs.analyzer_version != method.get(obs.checkpoint_id)
            for obs in item.observations.values()
        ):
            blockers.append("inspection_method_unknown")
        if any("uncertain_source_time" in entry["issues"] for entry in inputs):
            unknowns.append("causal_order_not_proven")
        if any(
            entry["role"] != "excluded"
            and any(
                issue in entry["issues"]
                for issue in (
                    "control_cycle_unresolved",
                    "uncertain_source_time",
                    "item_or_assembly_link_unresolved",
                )
            )
            for entry in inputs
        ):
            blockers.append("unresolved_evidence")
        item.external_blockers = tuple(blockers)
        item.evidence_context = {
            "inputs": inputs,
            "unknowns": list(dict.fromkeys(unknowns + blockers)),
            "pinned_versions": {
                "work_order": str(order["revision"]),
                "route": order["route_version"],
                "control_plan": order["control_plan_version"],
                "design_composition": str(order["revision"]),
                "factual_composition": composition["snapshot_id"] if composition else None,
                "mapping": "demo-identity-v1",
                "rules": "unified-v1",
                "processor": "unified-v1",
            },
            "analyzer_versions": [
                {"record_id": entry["record_id"], "version": entry["analyzer_version"]}
                for entry in inputs
                if entry["event_type"] == "inspection_observed"
            ],
            "contradictions": contradictions,
            "completeness": completeness,
            "impact": {
                "policy": "impact-v1",
                "paths": impact,
                "composition_snapshot": composition["snapshot_id"] if composition else None,
            },
        }

    def _basis(self, state, workflow, item_id, target=None):
        self._context(state, workflow, item_id)
        item = workflow._items[item_id]
        saved = next(
            (
                basis
                for basis in item.bases.values()
                if basis.context_revision == item.context_revision and basis.target_id == target
            ),
            None,
        )
        return workflow._basis_view(saved) if saved else workflow.create_basis(item_id, target)

    def item(self, item_id):
        return self.execute(lambda state, workflow, _: self._item_view(state, workflow, item_id))

    def _card(self, state, workflow, item_id, card):
        basis = self._basis(state, workflow, item_id, card.nonconformity_id)
        item = workflow._items[item_id]
        return {
            "id": card.nonconformity_id,
            "item_id": item_id,
            "context_revision": item.context_revision,
            "basis_id": basis["basis_id"],
            "status": card.status,
            "resolution_status": card.resolution_status,
            "review_required": card.review_required,
            "record_ids": list(
                dict.fromkeys(item.observations[oid].record_id for oid in card.observation_ids)
            ),
            "cause": {
                "status": "unresolved",
                "category": "unknown",
                "record_ids": [],
                "limitations": [
                    "hypothesis layer is planned; temporal coincidence does not establish cause"
                ],
            },
            "decisions": [
                workflow._decision_view(d)
                for d in item.decisions
                if d.target_id == card.nonconformity_id
            ],
        }

    def _item_view(self, state, workflow, item_id):
        item = workflow._item(item_id)
        basis = self._basis(state, workflow, item_id)
        current = workflow.state(item_id)
        return {
            "item_id": item_id,
            "context_revision": item.context_revision,
            "control_cycle_id": item.cycle_id,
            "basis_id": basis["basis_id"],
            "disposition": current["disposition"],
            "release_allowed_now": current["release_allowed_now"],
            "review_required": item.review_required or item.disposition_review_required,
            "blockers": list(current["blockers"]),
            "completeness": list(basis["completeness"]),
            "nonconformities": [
                self._card(state, workflow, item_id, card) for card in item.nonconformities.values()
            ],
            "decisions": [
                workflow._decision_view(d) for d in [*item.decisions, *item.dispositions]
            ],
        }

    def basis(self, basis_id):
        def read(_, workflow, __):
            for item in workflow._items.values():
                if basis_id in item.bases:
                    return workflow._basis_view(item.bases[basis_id])
            raise UnknownEntity("Unknown basis.")

        return self.execute(read)

    def list_items(self):
        return self.execute(
            lambda state, workflow, _: {
                "items": [self._item_view(state, workflow, key) for key in workflow._items],
                "next_cursor": None,
            }
        )

    def command(self, kind, target, body, actor):
        def apply(state, workflow, _):
            key = json.dumps([actor, kind, target, body["idempotency_key"]])
            signature = digest(body)
            if key in state["commands"]:
                cached = state["commands"][key]
                if cached["digest"] != signature:
                    raise IdempotencyConflict("Same successful key, different command.")
                return 200, cached["result"]
            item_id = (
                target
                if kind in {"disposition", "cycle"}
                else workflow._nonconformity(target)[0].item_id
            )
            item = workflow._items[item_id]
            basis = item.bases.get(body["expected_basis_id"])
            if not basis or basis.target_id != (
                None if kind in {"disposition", "cycle"} else target
            ):
                raise BasisMismatch("Basis target mismatch.")
            kwargs = {
                name: body[name]
                for name in ("reason", "expected_revision", "expected_basis_id", "idempotency_key")
            }
            kwargs["actor_id"] = actor
            if kind == "decision":
                if body.get("cause"):
                    raise ValueError(
                        "Established cause commands are not implemented in this prototype."
                    )
                result = workflow.decide_nonconformity(target, action=body["decision"], **kwargs)
            elif kind == "disposition":
                result = workflow.dispose(target, action=body["disposition"], **kwargs)
            elif kind == "resolution":
                if body["action"] != "resolve":
                    raise ValueError("Reopen is planned; not implemented by the reference core.")
                observation_ids = [
                    obs.observation_id
                    for obs in item.observations.values()
                    if obs.record_id in body["control_record_ids"]
                    and obs.effective_result == "no_defect_signs"
                ]
                if {item.observations[oid].record_id for oid in observation_ids} != set(
                    body["control_record_ids"]
                ):
                    raise ValueError(
                        "Control record is missing, foreign or not a valid negative control."
                    )
                result = workflow.resolve_nonconformity(
                    target,
                    rework_operation_id=body["rework_operation_run_id"],
                    previous_operation_run_id=body["previous_operation_run_id"],
                    observation_ids=observation_ids,
                    **kwargs,
                )
            elif kind == "cycle":
                if (
                    not item.dispositions
                    or item.dispositions[-1].decision_id != body["rework_disposition_id"]
                ):
                    raise ValueError("New cycle must refer to the current rework disposition.")
                if any(
                    details["disposition_id"] == body["rework_disposition_id"]
                    for details in state["cycles"][item_id].values()
                ):
                    raise ValueError("This rework assignment already has a control cycle.")
                cycle = f"cycle-{len(state['cycles'][item_id]) + 1}"
                result = workflow.start_cycle(item_id, cycle_id=cycle, **kwargs)
                state["cycles"][item_id][cycle] = {
                    "runs": body["planned_operation_run_ids"],
                    "disposition_id": body["rework_disposition_id"],
                }
                result = {"control_cycle_id": cycle, "context_revision": result["context_revision"]}
            else:
                raise ValueError("Unknown command")
            self._context(state, workflow, item_id)
            state["commands"][key] = {"digest": signature, "result": result}
            return 201, result

        return self.execute(apply)

    def _wire(self, state, workflow):
        for item in workflow._items.values():
            order = next(
                value for value in state["orders"].values() if item.item_id in value["item_ids"]
            )
            for message in item.outbox.values():
                if message.message_id in state["wire"]:
                    continue
                payload = message.payload
                decisions = {d.decision_id: d for d in [*item.decisions, *item.dispositions]}
                cards = []
                for card in payload["nonconformities"]:
                    if card["status"] != "confirmed":
                        continue
                    decision_id = card["decision_ids"][-1]
                    resolution_id = card["resolution_ids"][-1] if card["resolution_ids"] else None
                    cards.append(
                        {
                            "nonconformity_id": card["nonconformity_id"],
                            "resolution_status": card["resolution_status"],
                            "decision_id": decision_id,
                            "resolution_id": resolution_id,
                            "cause_status": "unresolved",
                            "cause_category": "unknown",
                            "basis_ids": list(
                                dict.fromkeys(
                                    decisions[did].basis_id
                                    for did in [decision_id, resolution_id]
                                    if did
                                )
                            ),
                        }
                    )
                body = {
                    "producer_id": self.namespace,
                    "external_system": "erp-demo",
                    "external_order_id": order["external_order_id"],
                    "external_item_id": item.item_id,
                    "message_id": message.message_id,
                    "result_version": message.result_version,
                    "previous_message_id": message.previous_message_id,
                    "context_revision": message.context_revision,
                    "control_cycle_id": payload["control_cycle_id"],
                    "disposition": payload["disposition"],
                    "release_allowed_now": payload["release_allowed_now"],
                    "blockers": payload["blockers"],
                    "review_required": payload["review_required"]
                    or payload["disposition_review_required"],
                    "confirmed_nonconformities": cards,
                }
                state["wire"][message.message_id] = {
                    "body": body,
                    "content_digest": digest(body),
                    "status": "pending",
                    "ack": None,
                }

    def deliveries(self):
        return self.execute(
            lambda state, _, __: {
                "items": [
                    {
                        "message_id": key,
                        "result_version": value["body"]["result_version"],
                        "status": value["status"],
                        "attempts": 0,
                        "last_error": None,
                    }
                    for key, value in state["wire"].items()
                ],
                "next_cursor": None,
            }
        )

    def pending(self):
        return self.execute(
            lambda state, _, __: [
                copy.deepcopy(value["body"])
                for value in state["wire"].values()
                if value["status"] == "pending"
            ]
        )

    def acknowledge(self, ack):
        from contracts.validation import validate

        validate("Ack", ack, external=True)

        def apply(state, _, __):
            saved = state["wire"].get(ack["message_id"])
            if not saved or ack["content_digest"] != saved["content_digest"]:
                raise AckMismatch("ACK digest/ID mismatch.")
            if any(
                ack[key] != saved["body"][key]
                for key in (
                    "producer_id",
                    "external_system",
                    "external_order_id",
                    "external_item_id",
                    "result_version",
                )
            ):
                raise AckMismatch("ACK stream/version mismatch.")
            saved.update(status="acknowledged", ack=copy.deepcopy(ack))
            return ack

        return self.execute(apply)

    def analytics(self, item_ids=None):
        def read(_, workflow, __):
            result = workflow.analytics(item_ids)
            return {
                "item_ids": list(result["item_ids"]),
                "computed_at": now().isoformat(),
                "counts": result["kpi"],
            }

        return self.execute(read)

    def timeline(self, item_id):
        return self.execute(
            lambda state, _, __: {
                "items": sorted(
                    [
                        state["refs"][entry["record_id"]]
                        for entry in state["relations"].get(item_id, [])
                    ],
                    key=lambda x: (x["occurred_at"], x["source_id"], x["event_id"]),
                ),
                "next_cursor": None,
            }
        )

    def raw(self, record_id):
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(raw_events).where(raw_events.c.record_id == UUID(record_id))
                )
                .mappings()
                .one_or_none()
            )
            if not row:
                raise UnknownEntity("Raw evidence is unavailable.")
            record = _record(row)
            return json.loads(self.protector.open(record.protection, record.metadata))
