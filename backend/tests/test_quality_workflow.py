from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors import (
    AckMismatch,
    IdempotencyConflict,
    ReleaseBlocked,
    StaleRevision,
)
from app.domain.quality import InMemoryErpReceiver, QualityWorkflow


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 26, tzinfo=UTC)

    def __call__(self):
        self.value += timedelta(seconds=1)
        return self.value


def opened_workflow():
    workflow = QualityWorkflow(Clock())
    workflow.open_item("assembly-001", required_checkpoints=("incoming", "final"))
    return workflow


def basis(workflow, item_id="assembly-001", target_id=None):
    return workflow.create_basis(item_id, target_id=target_id)


def test_stale_basis_rejects_old_decision_after_late_observation():
    workflow = opened_workflow()
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="defect-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    item = workflow.state("assembly-001")
    nonconformity_id = item["nonconformities"][0]["nonconformity_id"]
    old_basis = basis(workflow, target_id=nonconformity_id)
    old_revision = old_basis["context_revision"]
    workflow.record_observation(
        "assembly-001",
        observation_id="late-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    with pytest.raises(StaleRevision):
        workflow.decide_nonconformity(
            nonconformity_id,
            action="confirm",
            reason="late review",
            actor_id="inspector-1",
            expected_revision=old_revision,
            expected_basis_id=old_basis["basis_id"],
            idempotency_key="decision-1",
        )
    assert workflow.state("assembly-001")["kpi"]["confirmed_defects"] == 0


def test_confirmed_defect_is_preserved_and_late_evidence_requires_review():
    workflow = opened_workflow()
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="defect-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    nc = workflow.state("assembly-001")["nonconformities"][0]["nonconformity_id"]
    current = basis(workflow, target_id=nc)
    workflow.decide_nonconformity(
        nc,
        action="confirm",
        reason="visible mark",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="confirm-1",
    )
    before = workflow.state("assembly-001")
    workflow.record_observation(
        "assembly-001",
        observation_id="late-2",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    after = workflow.state("assembly-001")
    assert after["kpi"]["confirmed_defects"] == 1
    assert after["review_required"] is True
    assert (
        after["nonconformities"][0]["decision_ids"] == before["nonconformities"][0]["decision_ids"]
    )
    assert "context_review_required" in after["blockers"]


def test_idempotent_command_repeats_without_new_decision_or_outbox():
    workflow = opened_workflow()
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="defect-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    nc = workflow.state("assembly-001")["nonconformities"][0]["nonconformity_id"]
    current = basis(workflow, target_id=nc)
    kwargs = dict(
        action="confirm",
        reason="visible mark",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="confirm-1",
    )
    first = workflow.decide_nonconformity(nc, **kwargs)
    outbox_count = len(workflow.state("assembly-001")["outbox"])
    second = workflow.decide_nonconformity(nc, **kwargs)
    assert second == first
    assert len(workflow.state("assembly-001")["outbox"]) == outbox_count
    with pytest.raises(IdempotencyConflict):
        workflow.decide_nonconformity(nc, **{**kwargs, "reason": "changed reason"})


def test_release_requires_complete_control_and_resolution_then_allows_separate_release():
    workflow = opened_workflow()
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="defect-1",
        checkpoint_id="final",
        result="defect_signs",
        defect_ref="mark-001",
        defect_type="surface-mark",
    )
    nc = workflow.state("assembly-001")["nonconformities"][0]["nonconformity_id"]
    current = basis(workflow, target_id=nc)
    with pytest.raises(ReleaseBlocked) as error:
        workflow.dispose(
            "assembly-001",
            action="released",
            reason="try too early",
            actor_id="inspector-1",
            expected_revision=current["context_revision"],
            expected_basis_id=current["basis_id"],
            idempotency_key="release-early",
        )
    assert any("nonconformity" in blocker for blocker in error.value.blockers)
    workflow.decide_nonconformity(
        nc,
        action="confirm",
        reason="visible mark",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="confirm-1",
    )
    current = basis(workflow)
    workflow.dispose(
        "assembly-001",
        action="rework",
        reason="replace marked part",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="rework-1",
    )
    current = basis(workflow)
    workflow.start_cycle(
        "assembly-001",
        cycle_id="cycle-2",
        actor_id="master-1",
        reason="assigned rework",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="cycle-2",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-2",
        checkpoint_id="incoming",
        result="no_defect_signs",
        cycle_id="cycle-2",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="final-2",
        checkpoint_id="final",
        result="no_defect_signs",
        cycle_id="cycle-2",
    )
    current = basis(workflow, target_id=nc)
    workflow.resolve_nonconformity(
        nc,
        reason="repeat control is good after replacement",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="resolve-1",
        rework_operation_id="rework-run-2",
        previous_operation_run_id="run-1",
        observation_ids=("final-2",),
    )
    current = basis(workflow)
    released = workflow.dispose(
        "assembly-001",
        action="released",
        reason="all required checks passed",
        actor_id="inspector-1",
        expected_revision=current["context_revision"],
        expected_basis_id=current["basis_id"],
        idempotency_key="release-1",
    )
    state = workflow.state("assembly-001")
    assert released["action"] == "released"
    assert state["release_allowed_now"] is True
    assert state["kpi"] == {
        "confirmed_defects": 1,
        "open_confirmed_defects": 0,
        "resolved_confirmed_defects": 1,
        "open_nonconforming_items": 0,
        "review_required_items": 0,
        "rework_operations": 1,
    }


def test_outbox_is_full_snapshot_and_ack_is_idempotent_at_receiver():
    workflow = opened_workflow()
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        "assembly-001", observation_id="final-1", checkpoint_id="final", result="no_defect_signs"
    )
    messages = workflow.state("assembly-001")["outbox"]
    newest = messages[-1]
    receiver = InMemoryErpReceiver()
    first = receiver.receive(type("Message", (), newest)())
    assert first["application_status"] == "applied"
    duplicate = receiver.receive(type("Message", (), newest)())
    assert duplicate["status"] == "duplicate"
    workflow.acknowledge(
        newest["message_id"],
        result_version=newest["result_version"],
        content_digest=newest["content_digest"],
        application_status=first["application_status"],
    )
    with pytest.raises(AckMismatch):
        workflow.acknowledge(
            newest["message_id"],
            result_version=newest["result_version"],
            content_digest="wrong",
            application_status="applied",
        )


def test_erp_receiver_keeps_newer_version_when_delivery_is_reordered():
    workflow = opened_workflow()
    workflow.record_observation(
        "assembly-001",
        observation_id="incoming-1",
        checkpoint_id="incoming",
        result="no_defect_signs",
    )
    workflow.record_observation(
        "assembly-001",
        observation_id="final-1",
        checkpoint_id="final",
        result="no_defect_signs",
    )
    messages = workflow.state("assembly-001")["outbox"]
    receiver = InMemoryErpReceiver()
    newest = type("Message", (), messages[-1])()
    oldest = type("Message", (), messages[0])()
    assert receiver.receive(newest)["application_status"] == "applied"
    assert receiver.receive(oldest)["application_status"] == "superseded"
    assert receiver.stream_versions["assembly-001"] == newest.result_version
    assert receiver.receive(newest)["status"] == "duplicate"
