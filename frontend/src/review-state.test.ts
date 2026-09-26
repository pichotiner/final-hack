import { describe, expect, it } from "vitest";
import { basisDiff, initialState, reviewReducer, type Pinned } from "./review-state";

const pin: Pinned = {
  targetId: null,
  item: { item_id: "a", context_revision: 5, control_cycle_id: "c1", basis_id: "b5",
    disposition: "rework", release_allowed_now: false, review_required: false,
    blockers: ["open"], completeness: [], nonconformities: [], decisions: [] },
  basis: { basis_id: "b5", item_id: "a", target_id: null, context_revision: 5,
    control_cycle_id: "c1", created_at: "2026-09-26T10:00:00Z",
    inputs: [{ record_id: "r1", source_id: "s1", event_id: "e1", role: "used", reason: null,
      content_digest: "a".repeat(64), occurred_at: "2026-09-26T09:00:00Z", received_at: "2026-09-26T09:00:01Z", observation_ids: [] }],
    pinned_versions: { route: "v1", work_order: "1", control_plan: "1", design_composition: null,
      factual_composition: null, mapping: "1", rules: "1", processor: "1", links: null }, decision_ids: [], completeness: [],
    analysis_snapshot: { observations: [], nonconformities: [], scope_constraints: {}, kpi: {} },
    previous_decisions: [], excluded_inputs: [], contradictions: [], analyzer_versions: [],
    blockers: ["open"], unknowns: [], media_available: false },
};

describe("Pinned review and uncertain delivery", () => {
  it("polling cannot replace or mix the displayed basis and blocks a fresh stale command", () => {
    const opened = reviewReducer(initialState, { type: "pin", value: pin });
    const polled = reviewReducer(opened, { type: "poll", revision: 6 });
    expect(polled.pinned).toEqual(pin);
    expect(reviewReducer(polled, { type: "prepare", command: { path: "/api/v2/decision", body: "old" } })).toBe(polled);
    expect(reviewReducer(polled, { type: "poll", revision: 5 }).latestRevision).toBe(6);
  });

  it("lost response retains exact bytes/key despite a new revision; context cannot be silently replaced", () => {
    const body = JSON.stringify({ expected_revision: 5, expected_basis_id: "b5", idempotency_key: "stable", reason: "original" });
    let state = reviewReducer(initialState, { type: "pin", value: pin });
    state = reviewReducer(state, { type: "prepare", command: { path: "/api/v2/items/a/dispositions", body } });
    state = reviewReducer(state, { type: "unknown" });
    state = reviewReducer(state, { type: "poll", revision: 7 });
    expect(reviewReducer(state, { type: "clear" })).toBe(state);
    expect(reviewReducer(state, { type: "pin", value: pin })).toBe(state);
    expect(reviewReducer(state, { type: "retry" }).pending?.body).toBe(body);
  });

  it("a foreign or mismatched basis cannot be pinned to the displayed item", () => {
    expect(reviewReducer(initialState, { type: "pin", value: { ...pin, basis: { ...pin.basis, item_id: "b" } } })).toBe(initialState);
    expect(reviewReducer(initialState, { type: "pin", value: { ...pin, basis: { ...pin.basis, context_revision: 6 } } })).toBe(initialState);
  });

  it("diff includes added evidence, role changes and pinned version changes", () => {
    const next = { ...pin.basis, basis_id: "b6", context_revision: 6,
      inputs: [ { ...pin.basis.inputs[0], role: "contradictory" as const },
        { ...pin.basis.inputs[0], record_id: "late", source_id: "s1", event_id: "e2", role: "used" as const, reason: null } ],
      pinned_versions: { ...pin.basis.pinned_versions, route: "v2" } };
    const diff = basisDiff(pin.basis, next);
    expect(diff.added.map(x => x.record_id)).toEqual(["late"]);
    expect(diff.changedRoles.map(x => x.record_id)).toEqual(["r1"]);
    expect(diff.changedVersions).toEqual(["route"]);
  });

  it("a denied retry cannot prove rejection of the first unknown command", () => {
    const command = { path: "/api/v2/items/a/dispositions", body: "original" };
    let state = reviewReducer(initialState, { type: "pin", value: pin });
    state = reviewReducer(state, { type: "prepare", command });
    state = reviewReducer(state, { type: "unknown" });
    state = reviewReducer(state, { type: "retry" });
    state = reviewReducer(state, { type: "rejected" });
    expect(state.phase).toBe("unknown");
    expect(state.pending).toEqual(command);
    state = reviewReducer(state, { type: "retry" });
    state = reviewReducer(state, { type: "success" });
    expect(state.phase).toBe("succeeded");
  });
});
