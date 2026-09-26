"""Independent acceptance of PI-01 rules; not PostgreSQL/runtime acceptance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scripts.production_improvements import (
    AlarmEvent,
    Health,
    Installation,
    Link,
    Method,
    Run,
    Sample,
    Task,
    accept_handoff,
    alarm_projection,
    early_warning,
    lot_exposure,
    next_checks,
    prepare_handoff,
    readiness,
    signal_exposure,
    stage_map,
    tool_passport,
)

T = datetime(2026, 9, 26, 10, tzinfo=UTC)
KEY = ("sensor", "M1", "vibration", "rule-v1", "run-B")


def test_post_ready_only_with_all_six_fresh_positive_checks():
    health = Health("health-1", "post-1", T, T + timedelta(minutes=5), *([True] * 6))
    assert readiness(health, T)["status"] == "ready"
    for field in ("reference", "light", "focus", "channel", "calibration", "clock"):
        for value in (False, None):
            outcome = readiness(replace(health, **{field: value}), T)
            assert outcome["status"] == "unavailable" and field in outcome["issues"]
    assert readiness(health, T + timedelta(minutes=5))["issues"] == ["expired"]
    assert readiness(health, T - timedelta(seconds=1))["issues"] == ["future_measurement"]


def test_tool_change_does_not_reuse_old_baseline_or_double_installation():
    run = Run("run-B", "B", "M1", "slot1", T, T + timedelta(minutes=3), "steel", "P1")
    old = Installation("i1", "old", "M1", "slot1", T - timedelta(hours=1), T)
    new = Installation("i2", "new", "M1", "slot1", T, T + timedelta(hours=1))
    outcome = tool_passport(
        run,
        [old, new, new],
        history_complete=True,
        profile_key=("M1", "slot1", "old", "steel", "P1"),
    )
    assert outcome["tool"] == "new" and not outcome["baseline_applicable"]
    assert tool_passport(
        run, [new], history_complete=True, profile_key=("M1", "slot1", "new", "steel", "P1")
    )["baseline_applicable"]
    conflict = replace(new, evidence_id="i3", tool="other")
    assert (
        tool_passport(run, [new, conflict], history_complete=True, profile_key=())["status"]
        == "unknown"
    )
    assert tool_passport(run, [new], history_complete=False, profile_key=())["tool"] is None


def test_tool_change_inside_run_is_not_one_known_tool():
    run = Run("r", "B", "M1", "s", T, T + timedelta(minutes=3), "steel", "p")
    split = T + timedelta(minutes=1)
    installs = [
        Installation("i1", "a", "M1", "s", T, split),
        Installation("i2", "b", "M1", "s", split, run.end),
    ]
    assert tool_passport(run, installs, history_complete=True, profile_key=())[
        "possible_tools"
    ] == ["a", "b"]
    assert (
        tool_passport(run, installs, history_complete=True, profile_key=())["status"] == "unknown"
    )


def test_lot_closure_separates_exact_possible_and_unrelated_without_defects():
    edges = [Link("install-1", "part", "A", True), Link("install-2", "A", "B", False)]
    result = lot_exposure("lot1", ("part",), edges, ("part", "A", "B", "C"))
    assert result["exact"] == ["A", "part"]
    assert result["possible"] == ["B"]
    assert result["confirmed_defects_created"] == 0
    uncertain = lot_exposure("lot1", ("part",), edges, ("part", "A", "B", "C"), complete=False)
    assert uncertain["possible"] == ["B", "C"]
    assert result["possible"] == ["B"]  # previous snapshot unchanged


def test_historical_cycle_terminates_and_limit_never_returns_partial_scope():
    links = [Link("1", "A", "B", True), Link("2", "B", "A", True)]
    assert lot_exposure("lot", ("A",), links, ("A", "B"))["exact"] == ["A", "B"]
    with pytest.raises(ValueError, match="limit"):
        lot_exposure("lot", ("A",), links, ("A", "B"), limit=1)


def test_methods_must_be_approved_applicable_complete_and_discriminate():
    method = Method(
        "ultrasound",
        "1",
        "steel",
        "inside",
        True,
        (("material", "echo"), ("assembly", "normal"), ("geometry", "normal")),
        10,
    )
    bad = replace(method, method_id="camera", region="outside")
    unapproved = replace(method, method_id="experimental", approved=False)
    unhelpful = replace(
        method,
        method_id="same",
        outcomes=tuple((h, "same") for h in ("material", "assembly", "geometry")),
    )
    result = next_checks(
        ["material", "assembly", "geometry"],
        [method, bad, unapproved, unhelpful],
        material="steel",
        region="inside",
    )
    assert [x["method_id"] for x in result["methods"]] == ["ultrasound"]
    assert result["methods"][0]["worst_remaining_hypotheses"] == 2
    assert (
        next_checks(["material", "unknown"], [method], material="steel", region="inside")["status"]
        == "needs_technologist"
    )


def test_handoff_keeps_unknown_commands_deliveries_and_alarms_open():
    tasks = [
        Task("command", 1, "unknown_command", "master-1", T),
        Task("alarm", 3, "alarm", "master-1", T),
        Task("erp", 2, "delivery_pending", "master-1", T),
    ]
    snapshot = prepare_handoff("h1", tasks, "master-1", "master-2", T)
    late = Task("new", 1, "alarm", "master-1", T + timedelta(seconds=1))
    result = accept_handoff(
        snapshot, [*tasks, late], actor="master-2", now=T + timedelta(minutes=1)
    )
    assert result["accepted"] == ["alarm", "command", "erp"]
    assert result["not_transferred"] == ["new"]
    assert result["ages_seconds"]["command"] == 60
    assert all(task.open and task.owner == "master-1" for task in tasks)
    with pytest.raises(ValueError, match="recipient"):
        accept_handoff(snapshot, tasks, actor="stranger", now=T)
    with pytest.raises(ValueError, match="Stale"):
        accept_handoff(
            snapshot, [replace(tasks[0], revision=2), *tasks[1:]], actor="master-2", now=T
        )


def test_hundred_duplicates_one_episode_and_independent_problem_separate():
    first = AlarmEvent("e1", "w1", KEY, "raise", T)
    second = AlarmEvent("e2", "w2", (*KEY[:2], "coolant", *KEY[3:]), "raise", T)
    result = alarm_projection([first] * 100 + [second], T)
    assert result["active"] == ["w1", "w2"]
    assert result["sound_tokens"] == ["w1", "w2"]
    assert result["episodes"]["w1"]["evidence"] == ["e1"]


def test_alarm_ack_silences_without_closing_and_recovery_requires_reason():
    raised = AlarmEvent("1", "w", KEY, "raise", T)
    ack = AlarmEvent("2", "w", KEY, "acknowledge", T, "operator")
    evidence = AlarmEvent("3", "w", KEY, "evidence", T)
    result = alarm_projection([raised, ack, evidence], T)
    assert (
        result["sound_tokens"] == [] and result["active"] == ["w"] and result["light"] == "yellow"
    )
    close = AlarmEvent("5", "w", KEY, "close", T, "operator", "Tool inspected")
    with pytest.raises(ValueError, match="Recovery"):
        alarm_projection([raised, ack, close], T)
    recover = AlarmEvent("4", "w", KEY, "recover", T)
    assert alarm_projection([raised, ack, recover, close], T)["active"] == []


def test_late_alarm_has_no_new_sound_and_offline_is_not_green():
    event = AlarmEvent("e", "w", KEY, "raise", T)
    assert alarm_projection([event], T + timedelta(minutes=5))["sound_tokens"] == []
    assert alarm_projection([], T, monitoring_available=False)["light"] == "unavailable"
    assert alarm_projection([event], T, monitoring_available=False)["sound_tokens"] == []
    with pytest.raises(ValueError, match="different content"):
        alarm_projection([event, replace(event, kind="recover")], T)


def test_ewma_known_example_deduplicates_and_uses_only_matching_profile():
    rows = [
        Sample(str(i), T + timedelta(seconds=i), value, True, KEY)
        for i, value in enumerate([1.1, 1.3, 1.5, 1.6])
    ]
    result = early_warning(
        [*rows, rows[-1]], expected_key=KEY, baseline=1, alpha=0.3, threshold=1.2
    )
    assert [r["status"] for r in result] == ["normal", "normal", "normal", "warning"]
    assert [r["ewma"] for r in result] == pytest.approx([1.03, 1.111, 1.2277, 1.33939])
    invalid = replace(rows[-1], profile_key=("other-tool",))
    assert (
        early_warning([invalid], expected_key=KEY, baseline=1, alpha=0.3, threshold=1.2)[0][
            "status"
        ]
        == "unknown"
    )


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_missing_signal_cannot_become_zero_or_good(value):
    result = early_warning(
        [Sample("1", T, value, True, KEY)], expected_key=KEY, baseline=1, alpha=0.3, threshold=1.2
    )
    assert result == [{"event_id": "1", "status": "unknown", "ewma": None}]


def test_ewma_gap_breaks_consecutive_evidence_and_same_time_is_ambiguous():
    first = Sample("1", T, 3, True, KEY)
    after_gap = Sample("2", T + timedelta(minutes=1), 3, True, KEY)
    result = early_warning(
        [first, after_gap], expected_key=KEY, baseline=1, alpha=0.3, threshold=1.2, consecutive=2
    )
    assert [row["status"] for row in result] == ["normal", "normal"]
    with pytest.raises(ValueError, match="simultaneous"):
        early_warning(
            [first, replace(first, event_id="other")],
            expected_key=KEY,
            baseline=1,
            alpha=0.3,
            threshold=1.2,
        )


def test_map_has_denominators_deduplication_and_unknown_not_zero():
    a = {
        "cohort": "steel/P1/v1",
        "stage": "machining",
        "item": "A",
        "assessable": True,
        "detected": True,
        "origin_established": False,
    }
    b = {**a, "item": "B", "assessable": False, "detected": False}
    c = {**b, "stage": "assembly", "item": "C"}
    result = stage_map([a, a, b, c])
    assert result[0]["discovery_rate"] is None
    assert result[1]["processed"] == 2 and result[1]["coverage"] == 0.5
    assert result[1]["discovery_rate"] == 1 and result[1]["established_origin_rate"] == 0
    with pytest.raises(ValueError, match="different content"):
        stage_map([a, {**a, "detected": False}])


def test_late_sensor_window_expands_scope_b_to_a_b_but_not_other_machine():
    runs = [
        Run("a", "A", "M1", "s", T, T + timedelta(minutes=3), "steel", "P1"),
        Run("b", "B", "M1", "s", T + timedelta(minutes=3), T + timedelta(minutes=6), "steel", "P1"),
        Run("c", "C", "M2", "s", T, T + timedelta(minutes=6), "steel", "P1"),
    ]
    before = signal_exposure("M1", T + timedelta(minutes=4), T + timedelta(minutes=5), runs)
    after = signal_exposure("M1", T + timedelta(minutes=2), T + timedelta(minutes=5), runs)
    assert before["exact"] == ["B"] and after["exact"] == ["A", "B"]
    uncertain = signal_exposure(
        "M1", T + timedelta(minutes=3), T + timedelta(minutes=4), runs, clock_error_seconds=1
    )
    assert uncertain["exact"] == [] and uncertain["possible"] == ["A", "B"]
    assert (
        signal_exposure("M1", T, T + timedelta(minutes=1), [], complete=False)["coverage"]
        == "unknown"
    )
