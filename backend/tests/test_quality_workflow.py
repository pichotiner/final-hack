import copy
import hashlib
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import rfc8785

from app.domain.errors import (
    AckMismatch,
    BasisMismatch,
    IdempotencyConflict,
    InvalidTransition,
    ReleaseBlocked,
    StaleRevision,
)
from app.domain.quality import InMemoryErpReceiver, QualityWorkflow, _digest
from tests.quality_support import T0, Rig


def test_stale_basis_rejects_old_decision_without_side_effects():
    rig = Rig()
    rig.defect()
    old = rig.basis(rig.nc())
    rig.defect("late")
    before = rig.state()
    with pytest.raises(StaleRevision):
        rig.command(
            "decide_nonconformity",
            rig.nc(),
            action="confirm",
            expected_revision=old["context_revision"],
            expected_basis_id=old["basis_id"],
        )
    assert rig.state() == before


def test_decision_and_full_basis_remain_unchanged_after_late_evidence():
    rig = Rig()
    rig.defect()
    old = rig.basis(rig.nc())
    decision = rig.command(
        "decide_nonconformity",
        rig.nc(),
        action="confirm",
        expected_revision=old["context_revision"],
        expected_basis_id=old["basis_id"],
    )
    rig.defect("late")
    new = rig.basis(rig.nc())
    assert "late" not in old["observation_ids"]
    assert "late" in new["observation_ids"]
    assert old["context_snapshot"]["production_context"]["route_version"] == "route-1"
    assert old["analysis_snapshot"]["nonconformities"][0]["status"] == "pending_review"
    assert decision["decision_id"] in new["decision_ids"]
    assert "receipt-record" in new["evidence_record_ids"]
    assert rig.state()["kpi"]["confirmed_defects"] == 1
    assert rig.state()["review_required"]


def test_command_retry_precedes_revision_check_and_conflicting_body_is_rejected():
    rig = Rig()
    rig.defect()
    old = rig.basis(rig.nc())
    kwargs = dict(
        action="confirm",
        expected_revision=old["context_revision"],
        expected_basis_id=old["basis_id"],
        idempotency_key="stable-command",
    )
    result = rig.command("decide_nonconformity", rig.nc(), **kwargs)
    rig.defect("late")
    before = rig.state()
    assert rig.command("decide_nonconformity", rig.nc(), **kwargs) == result
    assert rig.state() == before
    with pytest.raises(IdempotencyConflict):
        rig.command("decide_nonconformity", rig.nc(), **kwargs, reason="different")


def test_complete_rework_resolution_release_and_late_review():
    rig = Rig()
    rig.observe("incoming-1", "incoming")
    rig.defect()
    rig.command("decide_nonconformity", rig.nc(), action="confirm")
    with pytest.raises(ReleaseBlocked):
        rig.command("dispose", action="released")
    rig.rework()
    rig.complete_rework()
    rig.observe("incoming-2", "incoming")
    rig.observe("final-2")
    rig.resolve()
    rig.command("dispose", action="released")
    state = rig.state()
    assert state["release_allowed_now"]
    assert state["kpi"]["confirmed_defects"] == 1
    assert state["kpi"]["open_confirmed_defects"] == 0
    assert state["kpi"]["resolved_confirmed_defects"] == 1
    assert state["kpi"]["rework_operations"] == 1
    old_decisions = state["nonconformities"][0]["decision_ids"]
    rig.defect("late", cycle_id="cycle-1")
    assert not rig.state()["release_allowed_now"]
    assert rig.state()["disposition"] == "released"
    assert rig.state()["nonconformities"][0]["decision_ids"] == old_decisions
    assert len(rig.state()["nonconformities"]) == 1


@pytest.mark.parametrize("quality", ["poor", "unknown"])
@pytest.mark.parametrize("result", ["defect_signs", "no_defect_signs"])
def test_bad_quality_never_covers_control_even_after_reject(quality, result):
    rig = Rig(checkpoints=("final",))
    if result == "defect_signs":
        rig.defect("bad", quality=quality)
        rig.command("decide_nonconformity", rig.nc(), action="reject")
    else:
        rig.observe("bad", quality=quality)
    assert "not_assessable:final" in rig.state()["blockers"]
    with pytest.raises(ReleaseBlocked):
        rig.command("dispose", action="released")
    rig.observe(
        "replacement", replaces_observation_id="bad", occurred_at=T0 + timedelta(minutes=11)
    )
    assert rig.state()["release_allowed_now"]


def test_missing_production_context_and_operations_block_release():
    workflow = QualityWorkflow()
    workflow.open_item("unknown", required_checkpoints=("final",), route_known=True)
    workflow.record_observation(
        "unknown",
        observation_id="o",
        checkpoint_id="final",
        result="no_defect_signs",
        cycle_id="cycle-1",
    )
    assert "production_context_missing" in workflow.state("unknown")["blockers"]
    rig = Rig(checkpoints=("final",))
    rig.observe("clean")
    rig.workflow.set_production_history(rig.item_id, {}, ())
    assert "operation_incomplete:run-1" in rig.state()["blockers"]
    assert "work_order_not_resolved" in rig.state()["blockers"]
    with pytest.raises(ReleaseBlocked):
        rig.command("dispose", action="released")


def test_used_cycle_and_reusing_rework_assignment_are_rejected():
    rig = Rig(checkpoints=("final",))
    rig.observe("old")
    rig.rework()
    before = rig.state()
    for cycle in ("cycle-1", "cycle-2", "cycle-3"):
        with pytest.raises(InvalidTransition):
            rig.command("start_cycle", cycle_id=cycle, operation_run_ids=("rework-2",))
    assert rig.state() == before
    with pytest.raises(ReleaseBlocked):
        rig.command("dispose", action="released")


def test_missing_cycle_is_unresolved_and_cannot_cover_current_checkpoint():
    rig = Rig(checkpoints=("final",))
    rig.observe("unbound", cycle_id=None)
    assert rig.state()["observations"][0]["cycle_id"] is None
    assert "observation_cycle_unresolved" in rig.state()["blockers"]
    assert "missing_checkpoint:final" in rig.state()["blockers"]


def test_defect_identity_survives_cycles_and_distinguishes_source_and_region():
    rig = Rig()
    rig.defect()
    rig.command("decide_nonconformity", rig.nc(), action="confirm")
    rig.rework()
    rig.defect("same-defect")
    assert len(rig.state()["nonconformities"]) == 1
    assert rig.state()["kpi"]["confirmed_defects"] == 1
    rig.defect("other-source", source_id="vision-2")
    rig.defect("other-region", region_id="face-B")
    rig.defect("no-ref-1", defect_ref=None)
    rig.defect("no-ref-2", defect_ref=None)
    assert len(rig.state()["nonconformities"]) == 5


def test_resolve_requires_actual_rework_and_event_time_after_completion():
    rig = Rig(checkpoints=("final",))
    rig.defect()
    rig.command("decide_nonconformity", rig.nc(), action="confirm")
    rig.rework()
    rig.observe("final-2", occurred_at=T0)
    with pytest.raises(InvalidTransition):
        rig.resolve()
    rig.complete_rework()
    before = rig.state()
    with pytest.raises(InvalidTransition):
        rig.resolve()
    assert rig.state() == before
    assert rig.state()["kpi"]["open_confirmed_defects"] == 1


def test_resolution_rejects_current_contradictory_defect():
    rig = Rig(checkpoints=("final",))
    rig.defect()
    rig.command("decide_nonconformity", rig.nc(), action="confirm")
    rig.rework()
    rig.complete_rework()
    rig.observe("final-2")
    rig.defect("still-present")
    with pytest.raises(InvalidTransition):
        rig.resolve()


def test_rework_operation_can_progress_but_not_change_identity():
    rig = Rig()
    rig.rework()
    rig.workflow.record_rework_operation(
        rig.item_id,
        operation_id="rework-1",
        previous_operation_run_id="run-2",
        cycle_id="cycle-2",
        status="in_progress",
    )
    rig.complete_rework()
    rig.complete_rework()
    assert rig.state()["kpi"]["rework_operations"] == 1
    with pytest.raises(InvalidTransition):
        rig.workflow.record_rework_operation(
            rig.item_id,
            operation_id="rework-1",
            previous_operation_run_id="run-1",
            cycle_id="cycle-2",
        )


def test_basis_target_is_exact_and_read_views_cannot_mutate_history():
    rig = Rig()
    rig.defect()
    card_basis = rig.basis(rig.nc())
    with pytest.raises(BasisMismatch):
        rig.command("dispose", action="rework", expected_basis_id=card_basis["basis_id"])
    before = copy.deepcopy(rig.state())
    rig.state()["outbox"][0]["payload"]["disposition"] = "tampered"
    assert rig.state() == before
    card_basis["context_snapshot"]["production_context"]["route_version"] = "tampered"
    stored = rig.workflow._item(rig.item_id).bases[card_basis["basis_id"]]
    assert stored.context_snapshot["production_context"]["route_version"] == "route-1"


def test_observation_duplicates_do_not_change_revision_outbox_or_cohort_counts():
    rig = Rig(checkpoints=("final",))
    rig.observe("clean")
    before = rig.state()
    rig.observe("clean")
    assert rig.state() == before
    result = rig.workflow.analytics((rig.item_id, rig.item_id))
    assert result["items"] == 1
    assert result["kpi"]["inspected_items"] == 1
    with pytest.raises(IdempotencyConflict):
        rig.observe("clean", analyzer_version="changed")


def test_erp_versions_conflicts_digest_and_full_ack():
    rig = Rig(checkpoints=("final",))
    rig.observe("one")
    messages = [SimpleNamespace(**value) for value in rig.state()["outbox"]]
    old, new = messages[0], messages[-1]
    receiver = InMemoryErpReceiver()
    ack = receiver.receive(new)
    assert ack["application_status"] == "applied"
    assert receiver.receive(old)["application_status"] == "superseded"
    assert receiver.receive(new)["status"] == "duplicate"
    assert rig.workflow.acknowledge(new.message_id, receipt=ack) == ack
    assert rig.workflow.acknowledge(new.message_id, receipt=receiver.receive(new)) == ack
    with pytest.raises(AckMismatch):
        rig.workflow.acknowledge(new.message_id, receipt={**ack, "result_version": True})
    with pytest.raises(AckMismatch):
        rig.workflow.acknowledge(new.message_id, receipt={**ack, "external_system": "wrong"})
    changed = copy.deepcopy(new)
    changed.payload["disposition"] = "tampered"
    with pytest.raises(AckMismatch):
        receiver.receive(changed)
    changed.content_digest = _digest(changed.payload)
    with pytest.raises(AckMismatch):
        receiver.receive(changed)
    changed = copy.deepcopy(old)
    changed.message_id = "new-id-same-old-version"
    changed.payload["message_id"] = changed.message_id
    changed.content_digest = _digest(changed.payload)
    with pytest.raises(IdempotencyConflict):
        receiver.receive(changed)


def test_digest_uses_rfc8785_number_and_unicode_canonicalization():
    value = {"number": 1.0, "unicode": "\u20ac", "nested": [1e-7, -0.0]}
    assert _digest(value) == hashlib.sha256(rfc8785.dumps(value)).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        {"analyzer_version": None},
        {"identification_status": "uncertain"},
        {"time_quality": "uncertain"},
        {"record_id": None},
    ],
)
def test_unknown_provenance_does_not_close_required_control(change):
    rig = Rig(checkpoints=("final",))
    rig.observe("unknown", **change)
    assert "not_assessable:final" in rig.state()["blockers"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"control_session_id": "source-chosen-cycle"}, "control_session_unresolved"),
        ({"inspection_phase": "incoming"}, "inspection_phase_mismatch"),
        ({"occurred_at": T0 + timedelta(minutes=3, seconds=30)}, "operation_time_not_applicable"),
    ],
)
def test_source_claim_phase_and_pre_operation_control_do_not_cover_final(change, reason):
    rig = Rig(checkpoints=("final",))
    rig.observe("invalid-final", **change)
    assert "not_assessable:final" in rig.state()["blockers"]
    basis = rig.basis()
    excluded = basis["control_assessments"][0]["excluded_inputs"][0]
    assert reason in excluded["reasons"]


def test_rework_control_must_reference_the_checkpoint_planned_run():
    rig = Rig(checkpoints=("final",))
    rig.defect()
    rig.command("decide_nonconformity", rig.nc(), action="confirm")
    rig.rework()
    rig.complete_rework()
    rig.observe("final-2", operation_run_id="run-2")
    assert "not_assessable:final" in rig.state()["blockers"]
    with pytest.raises(InvalidTransition):
        rig.resolve()


def test_component_scoped_defect_can_be_resolved_by_clean_checkpoint_scope():
    original = Rig(checkpoints=("final",)).context
    requirement = replace(original.controls[0], component_id="insert-1", region_id="face-a")
    scoped = replace(original, controls=(requirement,))
    rig = Rig(checkpoints=("final",), context_value=scoped)
    rig.defect()
    rig.command("decide_nonconformity", rig.nc(), action="confirm")
    rig.rework()
    rig.complete_rework()
    rig.observe("final-2")
    rig.resolve()
    assert rig.state()["kpi"]["resolved_confirmed_defects"] == 1
