"""Read the shared DTOs from pinned P4 snapshots, never from a newer analysis."""

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from app.adapters.postgres import _record, quality_constraints, quality_erp_imports, raw_events
from app.adapters.quality import _encode
from app.domain.errors import IntegrityViolation


def decision_view(value, bases):
    return {**value, "control_cycle_id": bases[value["basis_id"]]["cycle_id"]}


class SharedViews:
    def __init__(self, service):
        self.service = service
        self.quality = service.quality

    def _basis(self, basis, connection, decisions):
        snapshot = basis["context_snapshot"]
        context = snapshot.get("production_context") or {}
        analysis = basis["analysis_snapshot"]
        observations = analysis.get("observations", [])
        rules = {rule["checkpoint_id"]: rule for rule in context.get("controls", [])}
        assessments = []
        contradictory, selected, excluded = set(), set(), {}
        for part in basis["control_assessments"]:
            selected.update(part["selected_record_ids"])
            contradictory.update(part["contradicting_record_ids"])
            for entry in part["excluded_inputs"]:
                excluded.setdefault(entry["record_id"], []).extend(entry["reasons"])
            assessments.append(
                {
                    **part,
                    "control_cycle_id": part["cycle_id"],
                    "record_ids": list(dict.fromkeys(part["selected_record_ids"])),
                    "method_version": rules[part["checkpoint_id"]]["analyzer_version"],
                    "issues": []
                    if part["assessment_status"] == "assessable"
                    else [part["assessment_status"]],
                }
            )
        records = basis["evidence_record_ids"]
        rows = (
            connection.execute(
                select(raw_events).where(
                    raw_events.c.record_id.in_([UUID(value) for value in records])
                )
            ).mappings()
            if records
            else []
        )
        by_record = {str(row["record_id"]): row for row in rows}
        inputs = []
        for record_id in records:
            row = by_record.get(record_id)
            if row is None:
                raise IntegrityViolation("A pinned source record is unavailable.")
            self.service.journal._fact(_record(row))
            refs = [obs for obs in observations if obs.get("record_id") == record_id]
            role, reason = "used", "production_context_evidence"
            if record_id in contradictory:
                role, reason = "contradictory", "conflicting_inspection_results"
            elif record_id in selected:
                reason = "current_cycle_control"
            elif refs:
                role = "excluded"
                reason = ";".join(sorted(set(excluded.get(record_id, [])))) or (
                    "outside_current_cycle"
                    if all(obs["cycle_id"] != basis["cycle_id"] for obs in refs)
                    else "control_not_applicable"
                )
            inputs.append(
                {
                    "record_id": record_id,
                    **{key: row[key] for key in ("source_id", "event_id", "content_digest")},
                    "occurred_at": row["occurred_at"].isoformat(),
                    "received_at": row["received_at"].isoformat(),
                    "role": role,
                    "reason": reason,
                    "observation_ids": [obs["observation_id"] for obs in refs],
                }
            )
        return {
            **basis,
            "control_cycle_id": basis["cycle_id"],
            "inputs": inputs,
            "pinned_versions": {
                "work_order": context.get("work_order_version"),
                "route": context.get("route_version"),
                "control_plan": context.get("control_plan_version"),
                "design_composition": context.get("assembly_version"),
                "factual_composition": None,
                "mapping": context.get("mapping_version"),
                "rules": context.get("rules_version"),
                "processor": basis["processor_version"],
                "links": None,
            },
            "analysis_snapshot": {
                **analysis,
                "observations": [{"defect_index": None, **obs} for obs in observations],
            },
            "completeness": assessments,
            "unknowns": list(basis["blockers"]),
            "media_available": False,
            "previous_decisions": [decisions[key] for key in basis["decision_ids"]],
            "excluded_inputs": [entry for entry in inputs if entry["role"] == "excluded"],
            "contradictions": [
                {
                    "checkpoint_id": part["checkpoint_id"],
                    "record_ids": part["record_ids"],
                    "reason": "conflicting_inspection_results",
                }
                for part in assessments
                if part["assessment_status"] == "disputed"
            ],
            "analyzer_versions": [
                {"record_id": key, "version": version}
                for key, version in dict.fromkeys(
                    (obs["record_id"], obs["analyzer_version"])
                    for obs in observations
                    if obs["record_id"]
                )
            ],
        }

    def basis(self, basis_id):
        item_id = self.quality.find_basis_item(basis_id)
        return self.quality.read(
            item_id,
            lambda workflow, connection: self._basis_from_workflow(
                workflow, connection, item_id, basis_id
            ),
            basis_id=basis_id,
        )

    def _basis_from_workflow(self, workflow, connection, item_id, basis_id):
        item = workflow._item(item_id)
        bases = {key: _encode(value) for key, value in item.bases.items()}
        decisions = {
            value.decision_id: decision_view(_encode(value), bases)
            for value in [*item.decisions, *item.dispositions]
        }
        return self._basis(bases[basis_id], connection, decisions)

    def item(self, item_id, *, connection=None, target_ids=None):
        def read(workflow, connection):
            item = workflow._item(item_id)

            def basis_for(target=None):
                basis = next(
                    (
                        part
                        for part in item.bases.values()
                        if part.context_revision == item.context_revision
                        and part.target_id == target
                    ),
                    None,
                )
                if basis is None:
                    raise IntegrityViolation(
                        "Current basis unavailable; run the P7 storage upgrade."
                    )
                return self._basis_from_workflow(workflow, connection, item_id, basis.basis_id)

            basis = basis_for()
            state = _encode(workflow.state(item_id))
            bases = {key: _encode(value) for key, value in item.bases.items()}
            decisions = [
                decision_view(value, bases) for value in state["decisions"] + state["dispositions"]
            ]
            cards = []
            for nc in item.nonconformities.values():
                if target_ids is not None and nc.nonconformity_id not in target_ids:
                    continue
                card_basis = basis_for(nc.nonconformity_id)
                cards.append(
                    {
                        "id": nc.nonconformity_id,
                        "item_id": item_id,
                        "context_revision": item.context_revision,
                        "basis_id": card_basis["basis_id"],
                        "status": nc.status,
                        "resolution_status": nc.resolution_status,
                        "review_required": nc.review_required,
                        "record_ids": list(
                            dict.fromkeys(
                                obs.record_id
                                for obs in item.observations.values()
                                if workflow._candidate_key(obs) == nc.candidate_key
                                and obs.record_id
                            )
                        ),
                        "cause": {
                            "status": nc.cause_status,
                            "category": nc.cause_category,
                            "record_ids": list(nc.cause_evidence_record_ids),
                            "limitations": list(nc.cause_limitations),
                        },
                        "decisions": [
                            value
                            for value in decisions
                            if value["target_id"] == nc.nonconformity_id
                        ],
                    }
                )
            return {
                "item_id": item_id,
                "context_revision": item.context_revision,
                "control_cycle_id": item.cycle_id,
                "basis_id": basis["basis_id"],
                "disposition": state["disposition"],
                "release_allowed_now": state["release_allowed_now"],
                "review_required": item.review_required or item.disposition_review_required,
                "blockers": state["blockers"],
                "completeness": basis["completeness"],
                "nonconformities": cards,
                "decisions": decisions,
                "operations": item.production_history.get("operations", []),
                "timeline": item.production_history.get("timeline", []),
                "observations": state["effective_observations"],
                "cycles": item.cycles,
            }

        return self.quality.read(item_id, read, connection=connection)

    def scope_review(self):
        with self.quality.engine.connect() as connection:
            constraints = [
                {"constraint_id": row["constraint_id"], "reason": row["state"]["reason"]}
                for row in connection.execute(
                    select(quality_constraints).where(quality_constraints.c.is_open.is_(True))
                ).mappings()
            ]
        items = [self.item(state["item_id"]) for state in self.quality.list_states()]
        return {
            "constraints": constraints,
            "bases": {
                item["item_id"]: {
                    "basis_id": item["basis_id"],
                    "context_revision": item["context_revision"],
                }
                for item in items
            },
        }

    def command_result(self, result):
        value = _encode(dict(result))
        if "cycle_id" in value:
            return {**value, "control_cycle_id": value["cycle_id"]}
        basis = self.quality.get_basis(
            self.quality.find_basis_item(value["basis_id"]), value["basis_id"]
        )
        return {**value, "control_cycle_id": basis["cycle_id"]}

    def orders(self):
        with self.quality.engine.connect() as connection:
            rows = connection.execute(
                select(quality_erp_imports).order_by(quality_erp_imports.c.revision)
            ).mappings()
            orders = {}
            for row in rows:
                doc = row["document"]
                key = str(
                    uuid5(NAMESPACE_URL, repr((row["external_system"], row["external_order_id"])))
                )
                orders[key] = {
                    **doc,
                    "id": key,
                    "external_system": row["external_system"],
                    "item_ids": row["receipt"]["item_ids"],
                }
            return list(orders.values())
