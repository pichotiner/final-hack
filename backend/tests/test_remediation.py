import copy
from datetime import timedelta

import pytest
from starlette.requests import Request

from app.api.pagination import timeline_page
from app.domain.errors import InvalidEvent, InvalidTransition, StaleRevision
from tests.quality_support import T0, Rig


def pin_records(rig):
    item = rig.workflow._item(rig.item_id)
    rig.workflow.set_production_history(
        rig.item_id,
        item.production_history,
        [*item.evidence_record_ids, *(o.record_id for o in item.observations.values())],
    )


def test_poor_plus_good_is_not_assessable_until_explicit_repeat_and_old_basis_is_immutable():
    rig = Rig(checkpoints=("final",))
    rig.observe("poor", quality="poor")
    rig.observe("good", occurred_at=T0 + timedelta(minutes=11))
    pin_records(rig)
    basis = rig.basis()
    assert basis["control_assessments"][0]["assessment_status"] == "not_assessable"
    assert "not_assessable:final" in basis["blockers"]
    before = copy.deepcopy(basis)
    result = rig.command(
        "recover_control",
        action="replace_control",
        record_id="record-poor",
        replacement_record_id="record-good",
        idempotency_key="repeat",
    )
    assert rig.state()["release_allowed_now"]
    assert rig.basis()["control_assessments"][0]["assessment_status"] == "assessable"
    assert basis == before
    assert (
        rig.command(
            "recover_control",
            action="replace_control",
            record_id="record-poor",
            replacement_record_id="record-good",
            idempotency_key="repeat",
            expected_revision=result["reviewed_revision"],
            expected_basis_id=result["basis_id"],
        )
        == result
    )
    rig.command("dispose", action="released")


def test_rejected_false_sign_resolves_map_and_gate_but_not_poor_quality():
    rig = Rig(checkpoints=("final",))
    rig.defect()
    rig.observe("negative")
    assert rig.basis()["control_assessments"][0]["assessment_status"] == "disputed"
    decision = rig.command("decide_nonconformity", rig.nc(), action="reject")
    assessment = rig.basis()["control_assessments"][0]
    assert assessment["assessment_status"] == "assessable"
    assert assessment["resolution_decision_ids"] == [decision["decision_id"]]
    assert rig.state()["release_allowed_now"]


@pytest.mark.parametrize(
    "change",
    [
        {"region_id": "other"},
        {"analyzer_version": "other"},
        {"occurred_at": T0},
        {"quality": "poor"},
    ],
)
def test_incomparable_repeat_is_rejected_atomically(change):
    rig = Rig(checkpoints=("final",))
    rig.observe("poor", quality="poor")
    rig.observe("repeat", **change)
    pin_records(rig)
    before = rig.state()
    with pytest.raises(InvalidTransition):
        rig.command(
            "recover_control",
            action="replace_control",
            record_id="record-poor",
            replacement_record_id="record-repeat",
        )
    assert rig.state() == before


def test_unresolved_control_binds_without_rewriting_source_or_improving_quality():
    rig = Rig(checkpoints=("final",))
    rig.observe("unbound", cycle_id=None, control_session_id=None, quality="poor")
    pin_records(rig)
    before = rig.state()["observations"]
    assert rig.state()["kpi"]["repeat_controls"] == 0
    rig.command(
        "recover_control",
        action="bind_control",
        record_id="record-unbound",
        control_cycle_id="cycle-1",
    )
    assert rig.state()["observations"] == before
    assert "observation_cycle_unresolved" not in rig.state()["blockers"]
    assert "not_assessable:final" in rig.state()["blockers"]
    with pytest.raises(InvalidTransition):
        rig.command(
            "recover_control",
            action="bind_control",
            record_id="record-other",
            control_cycle_id="cycle-1",
        )


def test_inspected_lifetime_survives_new_cycle_and_repeat_counts_raw_inspections():
    rig = Rig(checkpoints=("final",))
    rig.observe("old")
    rig.rework()
    assert rig.state()["kpi"]["inspected_items"] == 1
    assert rig.state()["kpi"]["current_cycle_inspected_items"] == 0
    rig.complete_rework()
    rig.defect("sign-a", record_id="one-inspection", defect_ref="a")
    rig.defect("sign-b", record_id="one-inspection", defect_ref="b")
    assert rig.state()["kpi"]["repeat_controls"] == 1


def request(query):
    return Request({"type": "http", "query_string": query.encode()})


def test_timeline_pages_follow_event_time_ties_and_reject_late_insertion():
    entries = [
        {"record_id": record, "occurred_at": time, "source_id": "s", "event_id": event}
        for record, time, event in [
            ("a", "2026-09-26T11:00:00Z", "a"),
            ("f", "2026-09-26T10:00:00Z", "b"),
            ("e", "2026-09-26T10:00:00Z", "a"),
        ]
    ]
    first = timeline_page(request("limit=2"), "item", entries)
    assert [e["record_id"] for e in first["items"]] == ["e", "f"]
    page = request("limit=2&cursor=" + first["next_cursor"])
    assert timeline_page(page, "item", entries)["items"] == [entries[0]]
    with pytest.raises(StaleRevision):
        timeline_page(page, "item", entries[1:])
    with pytest.raises(InvalidEvent):
        timeline_page(request("cursor=garbage"), "item", entries)


def test_analytics_units_cause_denominator_period_and_explainable_circumstances():
    rig = Rig()
    history = copy.deepcopy(rig.workflow._item(rig.item_id).production_history)
    history["item_type_id"] = "bracket"
    for i, op in enumerate(history["operations"]):
        op.update(
            operation_type_id="assembly",
            station_id="assembly",
            line_id="line-1",
            operator_id="operator-1",
            shift_id="shift-1",
            evidence_record_ids=[f"op-{i}"],
        )
        op["durations"] = [
            {
                "kind": "station_elapsed",
                "origin": "calculated",
                "value": 60,
                "evidence_record_ids": [f"op-{i}"],
            },
            {
                "kind": "active_processing",
                "origin": "reported",
                "value": 40,
                "evidence_record_ids": [f"op-{i}"],
            },
        ]
    history["timeline"] = [
        {
            "record_id": rid,
            "operation_run_id": "run-2",
            "operator_id": "operator-1",
            "event_type": kind,
            "occurred_at": (T0 + timedelta(minutes=3, seconds=30)).isoformat(),
            "time_quality": "synchronized",
            "details": details,
            "station_id": "assembly",
            "line_id": "line-1",
        }
        for rid, kind, details in [
            ("action", "operator_action", {}),
            ("warning", "machine_state", {"state": "warning"}),
        ]
    ]
    rig.workflow.set_production_history(rig.item_id, history, ["action", "warning", "op-0", "op-1"])
    rig.observe("before", "incoming", occurred_at=T0)
    rig.defect("after")
    rig.defect("second-defect", defect_ref="other")
    for card in rig.state()["nonconformities"]:
        rig.command(
            "decide_nonconformity",
            card["nonconformity_id"],
            action="confirm",
            cause_status="established",
            cause_category="operator",
            cause_evidence_record_ids=["action"],
            operation_run_id="run-2",
            operator_id="operator-1",
            action_record_id="action",
        )
    result = rig.workflow.analytics()
    assert result["kpi"]["confirmed_defects"] == 2
    assert result["kpi"]["confirmed_operator_errors"] == 1
    assert result["comparable_work"][0]["completed_operations"] == 2
    assert result["comparable_work"][0]["confirmed_errors"] == 1
    assert result["comparable_work"][0]["error_rate"] == 0.5
    assert {(g["kind"], g["origin"], g["total_seconds"]) for g in result["durations"]} == {
        ("station_elapsed", "calculated", 120),
        ("active_processing", "reported", 80),
    }
    late = rig.workflow.analytics(since=T0 + timedelta(minutes=5), until=T0 + timedelta(minutes=11))
    assert len(late["defects"]) == 1
    assert late["durations"] == late["comparable_work"] == []
    analysis = rig.basis()["analysis_snapshot"]["circumstances"][0]
    assert analysis["before_record_id"] == "record-before"
    assert analysis["after_record_id"] == "record-after"
    assert {a["category"] for a in analysis["alternatives"]} == {"process", "equipment", "unknown"}
    assert analysis["status"] == "hypothesis"
