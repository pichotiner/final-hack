// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { api, ApiError, request, type Basis, type Item } from "./api";

vi.mock("./api", async importOriginal => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, request: vi.fn() };
});

const item: Item = { item_id: "a", context_revision: 5, control_cycle_id: "c1", basis_id: "b5",
  disposition: "pending", release_allowed_now: false, review_required: false,
  blockers: ["missing"], completeness: [], nonconformities: [], decisions: [] };
const basis: Basis = { basis_id: "b5", item_id: "a", target_id: null, context_revision: 5,
  control_cycle_id: "c1", created_at: "2026-09-26T10:00:00Z", inputs: [],
  pinned_versions: { route: "1", work_order: "1", control_plan: "1", design_composition: null,
    factual_composition: null, mapping: "1", rules: "1", processor: "1", links: null },
  analysis_snapshot: { observations: [], nonconformities: [], scope_constraints: {}, kpi: {} },
  previous_decisions: [], excluded_inputs: [], contradictions: [], analyzer_versions: [],
  decision_ids: [], completeness: [], blockers: ["missing"], unknowns: [], media_available: false };

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(request).mockReset();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(api, "items").mockResolvedValue({ items: [item], next_cursor: null });
  vi.spyOn(api, "item").mockResolvedValue(item);
  vi.spyOn(api, "basis").mockResolvedValue(basis);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

async function prepare() {
  render(<App />);
  await act(async () => { fireEvent.change(screen.getByLabelText(/Bearer token/), { target: { value: "token" } }); });
  await act(async () => { fireEvent.click(screen.getByText("Загрузить изделия")); });
  await act(async () => { fireEvent.change(screen.getByLabelText("Изделие"), { target: { value: "a" } }); });
  await act(async () => { fireEvent.click(screen.getByText("Начать / явно обновить рассмотрение")); });
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Действие"), { target: { value: "rework" } });
    fireEvent.change(screen.getByLabelText("Причина"), { target: { value: "repair" } });
  });
}

it("poll queued before submit prevents HTTP and leaves the component usable", async () => {
  await prepare();
  let complete!: (value: Item) => void;
  vi.mocked(api.item).mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
  await act(async () => { vi.advanceTimersByTime(5000); });
  await act(async () => {
    complete({ ...item, context_revision: 6 });
    await Promise.resolve();
    fireEvent.click(screen.getByText("Отправить отдельную команду"));
  });
  expect(request).not.toHaveBeenCalled();
  expect(screen.getByText(/Появилась ревизия 6/)).toBeTruthy();
  expect(screen.getByText("Начать / явно обновить рассмотрение")).toBeTruthy();
});

it("timeout then retry 403 retains exact body until the original receipt arrives", async () => {
  await prepare();
  vi.mocked(request).mockRejectedValueOnce(new ApiError("network_error", "lost response", true));
  await act(async () => { fireEvent.click(screen.getByText("Отправить отдельную команду")); });
  const original = vi.mocked(request).mock.calls[0];
  vi.mocked(request).mockRejectedValueOnce(new ApiError("permission_denied", "denied", false));
  await act(async () => { fireEvent.click(screen.getByText("Исход неизвестен: повторить точное тело и ключ")); });
  expect(vi.mocked(request).mock.calls[1]).toEqual(original);
  expect((screen.getByLabelText(/Bearer token/) as HTMLInputElement).disabled).toBe(true);
  const command = JSON.parse(original[2]!);
  vi.mocked(request).mockResolvedValueOnce({ decision_id: "original-decision", target_id: "a",
    context_revision: 6, basis_id: command.expected_basis_id });
  await act(async () => { fireEvent.click(screen.getByText("Исход неизвестен: повторить точное тело и ключ")); });
  expect(vi.mocked(request).mock.calls[2]).toEqual(original);
  expect(screen.getByRole("status").textContent).toContain("original-decision");
  expect((screen.getByLabelText(/Bearer token/) as HTMLInputElement).disabled).toBe(false);
});

it("a delayed basis cannot reopen a review after the user closed it", async () => {
  await prepare();
  let complete!: (value: Basis) => void;
  vi.mocked(api.basis).mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
  await act(async () => { fireEvent.click(screen.getByText("Начать / явно обновить рассмотрение")); });
  await act(async () => { fireEvent.click(screen.getByText("Завершить просмотр и сменить цель")); });
  await act(async () => { complete(basis); });
  expect(screen.queryByText(/B = b5/)).toBeNull();
  expect(request).not.toHaveBeenCalled();
});

it("shows server line state and does not treat a denied delivery read as an ACK", async () => {
  await prepare();
  vi.spyOn(api, "line").mockResolvedValue({ line_id: "line-1", computed_at: "2026-09-26T10:00:00Z",
    source_last_received_at: null, unresolved_items: 2, active_operation_run_ids: ["run-live"] });
  vi.spyOn(api, "deliveries").mockRejectedValue(new ApiError("permission_denied", "Нет прав на доставки"));
  await act(async () => { fireEvent.click(screen.getByText("Состояние линии")); });
  expect(screen.getByText(/Активные операции: run-live/)).toBeTruthy();
  await act(async () => { fireEvent.click(screen.getByText("Проверить доставки ERP")); });
  expect(screen.getByRole("status").textContent).toContain("Нет прав на доставки");
  expect(request).not.toHaveBeenCalled();
});
