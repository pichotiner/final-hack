"""PI-01 pure rules experiment. NOT a database, HTTP service or release authority.

Inputs are synthetic or supplied snapshots. Event lists are replayed, never persisted
here. The shared runtime must provide authentication, atomicity and protected storage.
"""

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta


def aware(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone is required")
    return value


def interval(start, end):
    if aware(start) >= aware(end):
        raise ValueError("Expected nonempty half-open interval")


def unique(values, key):
    result = {}
    for value in values:
        identity = key(value)
        if identity in result and result[identity] != value:
            raise ValueError("Identity reused with different content")
        result[identity] = value
    return list(result.values())


@dataclass(frozen=True)
class Health:
    evidence_id: str
    station: str
    measured_at: datetime
    expires_at: datetime
    reference: bool | None
    light: bool | None
    focus: bool | None
    channel: bool | None
    calibration: bool | None
    clock: bool | None


def readiness(snapshot: Health, now: datetime):
    interval(snapshot.measured_at, snapshot.expires_at)
    aware(now)
    checks = ("reference", "light", "focus", "channel", "calibration", "clock")
    if any(
        getattr(snapshot, key) is not None and type(getattr(snapshot, key)) is not bool
        for key in checks
    ):
        raise ValueError("Invalid health check")
    issues = [key for key in checks if getattr(snapshot, key) is not True]
    if now < snapshot.measured_at:
        issues.append("future_measurement")
    if now >= snapshot.expires_at:
        issues.append("expired")
    return {
        "station": snapshot.station,
        "evidence_id": snapshot.evidence_id,
        "status": "unavailable" if issues else "ready",
        "issues": issues,
    }


@dataclass(frozen=True)
class Run:
    run_id: str
    item: str
    equipment: str
    slot: str
    start: datetime
    end: datetime
    material: str
    program: str


@dataclass(frozen=True)
class Installation:
    evidence_id: str
    tool: str
    equipment: str
    slot: str
    start: datetime
    end: datetime


def tool_passport(run: Run, installations, *, history_complete: bool, profile_key: tuple):
    interval(run.start, run.end)
    relevant = []
    for entry in unique(installations, lambda x: x.evidence_id):
        interval(entry.start, entry.end)
        if (
            (entry.equipment, entry.slot) == (run.equipment, run.slot)
            and entry.start < run.end
            and run.start < entry.end
        ):
            relevant.append(entry)
    exact = (
        len(relevant) == 1
        and relevant[0].start <= run.start
        and relevant[0].end >= run.end
        and history_complete
    )
    tool = relevant[0].tool if exact else None
    return {
        "run_id": run.run_id,
        "tool": tool,
        "possible_tools": sorted({entry.tool for entry in relevant}),
        "evidence_ids": sorted(entry.evidence_id for entry in relevant),
        "status": "exact" if exact else "unknown",
        "baseline_applicable": exact
        and profile_key == (run.equipment, run.slot, tool, run.material, run.program),
    }


@dataclass(frozen=True)
class Link:
    evidence_id: str
    child: str
    parent: str
    certain: bool


def lot_exposure(
    lot: str,
    direct_members: tuple[str, ...],
    links,
    catalog: tuple[str, ...],
    *,
    possible_members=(),
    complete=True,
    limit=10000,
):
    """Conservative dependency closure over the supplied history snapshot.

    Caller must select applicable historical installation links. No current-only
    inference is made. 'Exact' means linked, never proven defective.
    """
    if not lot or limit < 1 or not direct_members and not possible_members and complete:
        raise ValueError("Missing lot/membership evidence")
    if len(set(catalog)) != len(catalog):
        raise ValueError("Duplicate catalog item")
    edges = unique(links, lambda x: x.evidence_id)
    referenced = set(direct_members) | set(possible_members)
    for edge in edges:
        referenced.update((edge.child, edge.parent))
    if not referenced <= set(catalog):
        raise ValueError("Reference outside supplied catalog")
    parents = {}
    for edge in edges:
        parents.setdefault(edge.child, []).append(edge)
    todo = deque([(x, True) for x in direct_members] + [(x, False) for x in possible_members])
    reached = set()
    exact, possible = set(), set()
    steps = 0
    while todo:
        node, certain = todo.popleft()
        if (node, certain) in reached:
            continue
        reached.add((node, certain))
        steps += 1
        if steps > limit:
            raise ValueError("Traversal limit: no partial exposure returned")
        (exact if certain else possible).add(node)
        for edge in parents.get(node, ()):
            steps += 1
            if steps > limit:
                raise ValueError("Traversal limit: no partial exposure returned")
            todo.append((edge.parent, certain and edge.certain))
    if not complete:
        possible.update(catalog)
    return {
        "lot": lot,
        "exact": sorted(exact),
        "possible": sorted(possible - exact),
        "coverage": "complete" if complete else "unknown",
        "action": "propose_check",
        "confirmed_defects_created": 0,
    }


@dataclass(frozen=True)
class Method:
    method_id: str
    version: str
    material: str
    region: str
    approved: bool
    # Possible outcome assigned by a technologist to each hypothesis.
    outcomes: tuple[tuple[str, str], ...]
    effort_minutes: int


def next_checks(hypotheses, methods, *, material, region):
    hypotheses = set(hypotheses)
    if len(hypotheses) < 2:
        return {"status": "needs_technologist", "methods": []}
    ranked = []
    for method in unique(methods, lambda x: (x.method_id, x.version)):
        if not method.approved or (method.material, method.region) != (material, region):
            continue
        if method.effort_minutes <= 0 or len(dict(method.outcomes)) != len(method.outcomes):
            raise ValueError("Invalid method matrix")
        outcomes = dict(method.outcomes)
        if not hypotheses <= outcomes.keys():
            continue
        groups = {}
        for hypothesis in sorted(hypotheses):
            groups.setdefault(outcomes[hypothesis], []).append(hypothesis)
        if len(groups) > 1:
            ranked.append(
                {
                    "method_id": method.method_id,
                    "version": method.version,
                    "worst_remaining_hypotheses": max(map(len, groups.values())),
                    "effort_minutes": method.effort_minutes,
                    "outcome_groups": groups,
                }
            )
    ranked.sort(
        key=lambda x: (x["worst_remaining_hypotheses"], x["effort_minutes"], x["method_id"])
    )
    return {"status": "suggestion" if ranked else "needs_technologist", "methods": ranked}


@dataclass(frozen=True)
class Task:
    task_id: str
    revision: int
    kind: str
    owner: str
    created_at: datetime
    open: bool = True


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    from_actor: str
    to_actor: str
    created_at: datetime
    tasks: tuple[Task, ...]


def prepare_handoff(handoff_id, tasks, from_actor, to_actor, now):
    if not all((handoff_id, from_actor, to_actor)) or from_actor == to_actor:
        raise ValueError("Explicit distinct actors required")
    selected = unique(tasks, lambda x: x.task_id)
    for task in selected:
        if aware(task.created_at) > aware(now) or task.revision < 1:
            raise ValueError("Invalid task snapshot")
    return Handoff(
        handoff_id,
        from_actor,
        to_actor,
        now,
        tuple(
            sorted(
                (t for t in selected if t.open and t.owner == from_actor), key=lambda x: x.task_id
            )
        ),
    )


def accept_handoff(snapshot, current_tasks, *, actor, now):
    if actor != snapshot.to_actor:
        raise ValueError("Only the named recipient may accept")
    if aware(now) < snapshot.created_at:
        raise ValueError("Acceptance cannot precede preparation")
    current = {t.task_id: t for t in unique(current_tasks, lambda x: x.task_id)}
    if any(current.get(task.task_id) != task for task in snapshot.tasks):
        raise ValueError("Stale handoff: refresh before accepting")
    new_open = [
        t.task_id
        for t in current.values()
        if t.open
        and t.owner == snapshot.from_actor
        and t.task_id not in {v.task_id for v in snapshot.tasks}
    ]
    # Immutable receipt: no task is closed or reassigned by mutating caller state.
    return {
        "handoff_id": snapshot.handoff_id,
        "actor": actor,
        "accepted_at": now.isoformat(),
        "accepted": [t.task_id for t in snapshot.tasks],
        "not_transferred": sorted(new_open),
        "ages_seconds": {t.task_id: (now - t.created_at).total_seconds() for t in snapshot.tasks},
    }


@dataclass(frozen=True)
class AlarmEvent:
    event_id: str
    episode: str
    key: tuple[str, str, str, str, str]  # source/equipment/channel/rule-version/run
    kind: str  # raise, evidence, acknowledge, recover, close
    at: datetime
    actor: str | None = None
    reason: str | None = None


def alarm_projection(events, now, *, fresh_seconds=30, monitoring_available=True):
    """Replay receipt order; late raises remain historical, not new sound commands."""
    aware(now)
    if fresh_seconds <= 0:
        raise ValueError("Freshness must be positive")
    states, active_keys, sounds = {}, {}, []
    for event in events:
        if len(event.key) != 5 or not all(event.key) or not event.episode:
            raise ValueError("Full episode identity required")
    for event in unique(events, lambda x: (x.key[0], x.event_id)):
        aware(event.at)
        if event.at > now:
            raise ValueError("Future alarm event")
        if event.kind == "raise":
            if event.episode in states or event.key in active_keys:
                raise ValueError("Use evidence for an existing episode")
            states[event.episode] = {
                "key": event.key,
                "acknowledged": False,
                "recovered": False,
                "closed": False,
                "evidence": [event.event_id],
            }
            active_keys[event.key] = event.episode
            if monitoring_available and (now - event.at).total_seconds() <= fresh_seconds:
                sounds.append(event.episode)
            continue
        state = states.get(event.episode)
        if state is None or state["key"] != event.key or state["closed"]:
            raise ValueError("Unknown, closed or mismatched episode")
        if event.kind == "evidence":
            state["evidence"].append(event.event_id)
        elif event.kind == "acknowledge":
            if not event.actor:
                raise ValueError("Actor required")
            state["acknowledged"] = True
        elif event.kind == "recover":
            state["recovered"] = True
        elif event.kind == "close":
            if not event.actor or not event.reason or not state["recovered"]:
                raise ValueError("Recovery, actor and reason required before closure")
            state["closed"] = True
            active_keys.pop(event.key)
        else:
            raise ValueError("Unknown alarm action")
    active = [key for key, state in states.items() if not state["closed"]]
    sound = [key for key in sounds if not states[key]["acknowledged"] and not states[key]["closed"]]
    return {
        "episodes": states,
        "active": active,
        "sound_tokens": sound,
        "light": "unavailable" if not monitoring_available else "yellow" if active else "green",
        "meaning": "monitoring_only_not_release",
    }


@dataclass(frozen=True)
class Sample:
    event_id: str
    at: datetime
    value: float | None
    good: bool
    profile_key: tuple


def early_warning(
    samples, *, expected_key, baseline, alpha, threshold, consecutive=2, max_gap_seconds=2
):
    if (
        not expected_key
        or not 0 < alpha <= 1
        or consecutive < 1
        or not all(map(math.isfinite, (baseline, threshold)))
    ):
        raise ValueError("Invalid EWMA profile")
    if not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0:
        raise ValueError("Invalid sampling gap")
    z, streak, rows, previous = baseline, 0, [], None
    values = unique(samples, lambda x: x.event_id)
    # This is retrospective analysis; runtime sound freshness is a separate rule.
    for sample in sorted(values, key=lambda x: (aware(x.at), x.event_id)):
        if previous is not None and sample.at == previous:
            raise ValueError("Ambiguous simultaneous measurements in one channel")
        if previous is not None and (sample.at - previous).total_seconds() > max_gap_seconds:
            z, streak = baseline, 0
        previous = sample.at
        valid = (
            sample.good
            and sample.profile_key == expected_key
            and sample.value is not None
            and math.isfinite(sample.value)
        )
        if not valid:
            z, streak = baseline, 0
            rows.append({"event_id": sample.event_id, "status": "unknown", "ewma": None})
            continue
        z = alpha * sample.value + (1 - alpha) * z
        streak = streak + 1 if z > threshold else 0
        rows.append(
            {
                "event_id": sample.event_id,
                "status": "warning" if streak >= consecutive else "normal",
                "ewma": z,
            }
        )
    return rows


def stage_map(rows):
    """One supplied row per item/stage/cohort; discovery != origin."""
    groups = {}
    for row in unique(rows, lambda x: (x["cohort"], x["stage"], x["item"])):
        if row["detected"] and not row["assessable"]:
            raise ValueError("Confirmed discovery requires assessable control")
        group = groups.setdefault(
            (row["cohort"], row["stage"]),
            {"processed": 0, "assessable": 0, "detected": 0, "origin_established": 0},
        )
        group["processed"] += 1
        for field in ("assessable", "detected", "origin_established"):
            if type(row[field]) is not bool:
                raise ValueError("Explicit boolean cohort fact required")
            group[field] += row[field]
    return [
        {
            "cohort": key[0],
            "stage": key[1],
            **g,
            "coverage": g["assessable"] / g["processed"],
            "discovery_rate": g["detected"] / g["assessable"] if g["assessable"] else None,
            "established_origin_rate": g["origin_established"] / g["processed"],
        }
        for key, g in sorted(groups.items())
    ]


def signal_exposure(equipment, start, end, runs, *, clock_error_seconds=0, complete=True):
    """Temporal overlap is exposure evidence, not evidence that damage occurred."""
    interval(start, end)
    if not math.isfinite(clock_error_seconds) or clock_error_seconds < 0:
        raise ValueError("Invalid clock uncertainty")
    margin = timedelta(seconds=clock_error_seconds)
    exact, possible = set(), set()
    for run in unique(runs, lambda x: x.run_id):
        interval(run.start, run.end)
        if run.equipment != equipment:
            continue
        if run.start < end and start < run.end and clock_error_seconds == 0:
            exact.add(run.item)
        elif run.start < end + margin and start - margin < run.end:
            possible.add(run.item)
    return {
        "exact": sorted(exact),
        "possible": sorted(possible - exact),
        "coverage": "complete" if complete else "unknown",
        "issue": None if complete else "additional_unlisted_runs_may_be_affected",
    }
