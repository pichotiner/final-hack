"""Explicit synthetic production facts shared by unit and PostgreSQL scenarios."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.control import ControlRequirement, ProductionContext, SourceAuthority
from app.domain.quality import QualityWorkflow

T0 = datetime(2026, 9, 26, 10, tzinfo=UTC)


def context(item_id="assembly-001", checkpoints=("incoming", "final")):
    return ProductionContext(
        work_order_id="order-1",
        work_order_version="1",
        route_version="route-1",
        assembly_version="bom-1",
        mapping_version="map-1",
        external_system="erp-demo",
        external_order_id="external-order-1",
        external_item_id=item_id,
        required_operations=("run-1", "run-2"),
        component_ids=("insert-1",),
        controls=tuple(
            ControlRequirement(
                cp,
                "vision-1",
                "analyzer-1",
                operation_run_id="run-2" if cp == "final" else None,
                inspection_phase="final" if cp == "final" else "incoming",
                control_session_id=f"cycle-1-{cp}",
            )
            for cp in checkpoints
        ),
        source_authorities=(
            SourceAuthority("erp-source", ("item_received",)),
            SourceAuthority("mes-source", ("operation_started", "operation_finished")),
            SourceAuthority("vision-1", ("inspection_observed",)),
            SourceAuthority("operator-source", ("operator_action",)),
            SourceAuthority("machine-source", ("machine_state",)),
        ),
    )


class Rig:
    def __init__(
        self,
        workflow=None,
        item_id="assembly-001",
        checkpoints=("incoming", "final"),
        context_value=None,
    ):
        self.workflow = workflow or QualityWorkflow()
        self.item_id = item_id
        self.context = context_value or context(item_id, checkpoints)
        self.workflow.open_item(item_id, context=self.context)
        self.workflow.set_production_history(
            item_id,
            {
                "work_order_id": "order-1",
                "assembly_version": "bom-1",
                "component_ids": ["insert-1"],
                "operations": [
                    {
                        "operation_run_id": "run-1",
                        "status": "completed",
                        "started_at": (T0 + timedelta(minutes=1)).isoformat(),
                        "finished_at": (T0 + timedelta(minutes=2)).isoformat(),
                        "issues": [],
                    },
                    {
                        "operation_run_id": "run-2",
                        "status": "completed",
                        "started_at": (T0 + timedelta(minutes=3)).isoformat(),
                        "finished_at": (T0 + timedelta(minutes=4)).isoformat(),
                        "issues": [],
                    },
                ],
            },
            ["receipt-record", "operation-record-1", "operation-record-2"],
        )

    def state(self):
        return self.workflow.state(self.item_id)

    def observe(self, oid, checkpoint="final", result="no_defect_signs", **kwargs):
        requirement = next(
            value for value in self.context.controls if value.checkpoint_id == checkpoint
        )
        values = {
            "cycle_id": self.state()["cycle_id"],
            "source_id": "vision-1",
            "analyzer_version": "analyzer-1",
            "record_id": f"record-{oid}",
            "occurred_at": T0 + timedelta(minutes=10),
            "time_quality": "synchronized",
            "identification_status": "reliable",
            "operation_run_id": (
                None
                if checkpoint == "incoming"
                else "rework-1"
                if self.state()["cycle_id"] == "cycle-2"
                else "run-2"
            ),
            "inspection_phase": "final" if checkpoint == "final" else "incoming",
            "control_session_id": f"{self.state()['cycle_id']}-{checkpoint}",
            "component_id": requirement.component_id,
            "region_id": requirement.region_id,
        }
        return self.workflow.record_observation(
            self.item_id,
            observation_id=oid,
            checkpoint_id=checkpoint,
            result=result,
            **{**values, **kwargs},
        )

    def defect(self, oid="defect-1", **kwargs):
        return self.observe(
            oid,
            result="defect_signs",
            **{
                "defect_ref": "mark-1",
                "defect_type": "surface-mark",
                **kwargs,
            },
        )

    def basis(self, target=None):
        return self.workflow.create_basis(self.item_id, target_id=target)

    def command(self, method, target=None, **kwargs):
        basis = self.basis(target)
        return getattr(self.workflow, method)(
            target or self.item_id,
            **{
                "actor_id": "inspector-1",
                "reason": "synthetic review",
                "expected_revision": basis["context_revision"],
                "expected_basis_id": basis["basis_id"],
                "idempotency_key": str(uuid4()),
                **kwargs,
            },
        )

    def nc(self):
        return self.state()["nonconformities"][0]["nonconformity_id"]

    def rework(self):
        self.command("dispose", action="rework")
        checkpoints = self.state()["required_checkpoints"]
        self.command(
            "start_cycle",
            cycle_id="cycle-2",
            operation_run_ids=("rework-1",),
            control_sessions={value: f"cycle-2-{value}" for value in checkpoints},
            checkpoint_operation_run_ids={
                value: ("rework-1" if value == "final" else None) for value in checkpoints
            },
        )

    def complete_rework(self):
        self.workflow.record_rework_operation(
            self.item_id,
            operation_id="rework-1",
            previous_operation_run_id="run-2",
            cycle_id="cycle-2",
            status="completed",
            finished_at=T0 + timedelta(minutes=5),
            time_quality="synchronized",
        )

    def resolve(self):
        return self.command(
            "resolve_nonconformity",
            self.nc(),
            rework_operation_id="rework-1",
            previous_operation_run_id="run-2",
            observation_ids=("final-2",),
        )
