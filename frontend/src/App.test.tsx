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
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.spyOn(api, "items").mockResolvedValue({ items: [item], next_cursor: null });
  vi.spyOn(api, "item").mockResolvedValue(item);
  vi.spyOn(api, "basis").mockResolvedValue(basis);
  vi.spyOn(api, "session").mockResolvedValue({ actor_id: "controller-1", roles: ["controller"] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

async function prepare() {
  render(<App />);
  await act(async () => { fireEvent.change(screen.getByLabelText(/Bearer token/), { target: { value: "token" } }); });
  await act(async () => { fireEvent.click(screen.getByText("Загрузить изделия")); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Открыть a" })); });
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

it("reauthenticates the same actor after a lost response and retries identical bytes with a new token", async () => {
  await prepare();
  vi.mocked(request).mockRejectedValueOnce(new ApiError("network_error", "lost response", true));
  await act(async () => { fireEvent.click(screen.getByText("Отправить отдельную команду")); });
  const original = vi.mocked(request).mock.calls[0];
  vi.mocked(api.session).mockResolvedValueOnce({ actor_id: "another-controller", roles: ["controller"] });
  await act(async () => { fireEvent.change(screen.getByLabelText("Новый токен той же личности"), { target: { value: "wrong" } }); });
  await act(async () => { fireEvent.click(screen.getByText("Восстановить доступ, сохранив команду")); });
  expect(screen.getByRole("status").textContent).toContain("Нужна та же личность");
  await act(async () => { fireEvent.change(screen.getByLabelText("Новый токен той же личности"), { target: { value: "renewed" } }); });
  await act(async () => { fireEvent.click(screen.getByText("Восстановить доступ, сохранив команду")); });
  const command = JSON.parse(original[2]!);
  vi.mocked(request).mockResolvedValueOnce({ decision_id: "one-decision", target_id: "a", context_revision: 6, basis_id: command.expected_basis_id });
  await act(async () => { fireEvent.click(screen.getByText("Исход неизвестен: повторить точное тело и ключ")); });
  expect(vi.mocked(request).mock.calls[1]).toEqual([original[0], "renewed", original[2]]);
  expect(screen.getByRole("status").textContent).toContain("one-decision");
});

it("shows server line state and does not treat a denied delivery read as an ACK", async () => {
  await prepare();
  vi.spyOn(api, "line").mockResolvedValue({ line_id: "line-1", computed_at: "2026-09-26T10:00:00Z",
    source_last_received_at: null, unresolved_items: 2, active_operation_run_ids: ["run-live"] });
  vi.spyOn(api, "deliveries").mockRejectedValue(new ApiError("permission_denied", "Нет прав на доставки"));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /Рабочее место ОТК/ })); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Состояние линии" })); });
  expect(screen.getByText(/Активные операции: run-live/)).toBeTruthy();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Обмен с ERP" })); });
  await act(async () => { fireEvent.click(screen.getByText("Проверить доставки ERP")); });
  expect(screen.getByRole("status").textContent).toContain("Нет прав на доставки");
  expect(request).not.toHaveBeenCalled();
});

it("keeps a pinned decision draft across navigation and only offers item actions", async () => {
  await prepare();
  expect(screen.queryByRole("option", { name: "Подтвердить дефект" })).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Аналитика качества" })); });
  expect(screen.queryByLabelText("Причина")).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Изделия" })); });
  expect((screen.getByLabelText("Причина") as HTMLTextAreaElement).value).toBe("repair");
  expect(screen.getByText(/Основания закреплены/)).toBeTruthy();
  expect(request).not.toHaveBeenCalled();
});

it("filters real items by review state and opens the selected item", async () => {
  vi.mocked(api.items).mockResolvedValue({ items: [item, { ...item, item_id: "late-item", review_required: true }], next_cursor: null });
  render(<App />);
  await act(async () => { fireEvent.change(screen.getByLabelText(/Bearer token/), { target: { value: "token" } }); });
  await act(async () => { fireEvent.click(screen.getByText("Загрузить изделия")); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Пересмотр" })); });
  expect(screen.queryByRole("button", { name: "Открыть a" })).toBeNull();
  expect(screen.getByRole("button", { name: "Открыть late-item" })).toBeTruthy();
  await act(async () => { fireEvent.change(screen.getByLabelText("Поиск изделия"), { target: { value: "no-match" } }); });
  expect(screen.getByText("Нет изделий по выбранным условиям")).toBeTruthy();
  await act(async () => { fireEvent.change(screen.getByLabelText("Поиск изделия"), { target: { value: "late" } }); });
  vi.mocked(api.item).mockResolvedValue({ ...item, item_id: "late-item", review_required: true });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Открыть late-item" })); });
  expect(api.item).toHaveBeenCalledWith("late-item", "token");
  expect(screen.getByText("Требуется пересмотр")).toBeTruthy();
});

it("a delayed comparison cannot replace the basis opened more recently", async () => {
  vi.mocked(api.item).mockResolvedValue({ ...item, decisions: [3, 4].map(revision => ({
    decision_id: `d${revision}`, target_id: "a", action: "rework" as const, reason: "repair",
    actor_id: "controller-1", basis_id: `b${revision}`, control_cycle_id: "c1",
    reviewed_revision: revision, context_revision: revision + 1,
    created_at: "2026-09-26T10:00:00Z", supersedes_id: null,
  })) });
  await prepare();
  let complete!: (value: Basis) => void;
  vi.mocked(api.basis).mockReturnValueOnce(new Promise(resolve => { complete = resolve; }));
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Предыдущее основание из истории решений"), { target: { value: "b4" } });
    fireEvent.click(screen.getByText("Открыть сохранённое основание"));
  });
  vi.mocked(api.basis).mockResolvedValueOnce({ ...basis, basis_id: "b3", context_revision: 3 });
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Предыдущее основание из истории решений"), { target: { value: "b3" } });
    fireEvent.click(screen.getByText("Открыть сохранённое основание"));
  });
  await act(async () => { complete({ ...basis, basis_id: "b4", context_revision: 4 }); });
  expect(screen.getByText("Сохранённое основание решения b3")).toBeTruthy();
  expect(screen.queryByText("Сохранённое основание решения b4")).toBeNull();
  expect(request).not.toHaveBeenCalled();
});
