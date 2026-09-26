// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { Basis } from "./api";
import { BasisExplanation, SuggestedChecks } from "./BasisExplanation";
import { explainBasis } from "./basis-explanation";

afterEach(cleanup);
const before: Basis = { basis_id: "b1", item_id: "A", target_id: null, context_revision: 1,
  control_cycle_id: "c1", created_at: "2026-09-26T10:00:00Z", inputs: [],
  pinned_versions: { work_order: "1", route: "1", control_plan: "1", design_composition: null,
    factual_composition: null, mapping: "1", rules: "1", processor: "1", links: null },
  analysis_snapshot: { observations: [], nonconformities: [], scope_constraints: {}, kpi: {} },
  decision_ids: [], previous_decisions: [], completeness: [], excluded_inputs: [], contradictions: [],
  analyzer_versions: [], blockers: [], unknowns: [], media_available: false };

it("explains late evidence and blockers without inventing a physical cause", () => {
  const after: Basis = { ...before, basis_id: "b2", context_revision: 2, blockers: ["late_review"],
    inputs: [{ record_id: "late-input", source_id: "camera", event_id: "event", content_digest: "a".repeat(64),
      occurred_at: "2026-09-26T09:00:00Z", received_at: "2026-09-26T10:01:00Z", observation_ids: [], role: "used", reason: null }] };
  render(<BasisExplanation before={before} after={after} />);
  expect(screen.getByText(/Новых исходников: 1/)).toBeTruthy();
  expect(screen.getByText(/Изменились: ограничения выпуска/)).toBeTruthy();
  expect(screen.getByText(/причина дефекта автоматически не устанавливается/)).toBeTruthy();
  expect(before.inputs).toHaveLength(0);
});

it("refuses cross-target, cross-item and reverse comparisons", () => {
  for (const after of [{ ...before, item_id: "B" }, { ...before, target_id: "card" }, { ...before, context_revision: 0 }]) {
    expect(explainBasis(before, after).comparable).toBe(false);
  }
});

it("order-only changes do not become an invented change of conditions", () => {
  const a = { ...before, blockers: ["a", "b"], unknowns: ["x", "y"] };
  const b = { ...a, blockers: ["b", "a"], unknowns: ["y", "x"] };
  expect(explainBasis(a, b).conditions).toContain("изменений не найдено");
});

it("gives separate suggestions for missing, poor and disputed control", () => {
  const point: Basis["completeness"][number] = { checkpoint_id: "incoming", requirement_version: "1", control_cycle_id: "c1",
    assessment_status: "missing", record_ids: [], issues: [], component_id: "part", region_id: "face",
    method_version: null, operation_run_id: null, control_session_id: "session", effective_results: [] };
  render(<SuggestedChecks basis={{ ...before, completeness: [point, { ...point, checkpoint_id: "poor", assessment_status: "not_assessable" },
    { ...point, checkpoint_id: "conflict", assessment_status: "disputed" }] }} />);
  expect(screen.getByText(/Выполнить отсутствующий/)).toBeTruthy();
  expect(screen.getByText(/Восстановить качество/)).toBeTruthy();
  expect(screen.getByText(/новый сигнал сам по себе не снимает/)).toBeTruthy();
  expect(screen.getByText(/не назначение проверки и не разрешение выпуска/)).toBeTruthy();
});
