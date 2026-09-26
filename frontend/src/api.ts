import type { components } from "./generated/api";

export type Item = components["schemas"]["Item"];
export type Basis = components["schemas"]["Basis"];
export type Card = components["schemas"]["Nonconformity"];
export type ReviewCommand = components["schemas"]["ReviewCommand"];
export type DispositionCommand = components["schemas"]["DispositionCommand"];
export type ResolutionCommand = components["schemas"]["ResolutionCommand"];
export type CycleCommand = components["schemas"]["CycleCommand"];
export type Analytics = components["schemas"]["Analytics"];
export type RecoveryCommand = components["schemas"]["ControlRecoveryCommand"];
export type ScopeCommand = components["schemas"]["ScopeCommand"];

export class ApiError extends Error {
  constructor(public code: string, message: string, public outcomeUnknown = false) {
    super(message);
  }
}

export function verifyCommandReceipt(path: string, body: string, result: Record<string, unknown>) {
  const command = JSON.parse(body);
  if (path.startsWith("/api/v2/scope-constraints/")) {
    const bases = result?.bases as Record<string, { basis_id: string; context_revision: number }> | undefined;
    if (!result || typeof result.decision_id !== "string" || result.constraint_id !== decodeURIComponent(path.split("/")[4]) ||
        !bases || Object.keys(bases).length !== Object.keys(command.bases).length ||
        Object.keys(command.bases).some(key => bases[key]?.basis_id !== command.bases[key].basis_id || bases[key]?.context_revision !== command.bases[key].context_revision)) {
      throw new ApiError("invalid_command_receipt", "Исход неизвестен: неверная квитанция разрешения области", true);
    }
    return;
  }
  const cycle = path.endsWith("/control-cycles");
  if (!result || !Number.isInteger(result.context_revision) ||
      (cycle ? typeof result.control_cycle_id !== "string" :
        typeof result.decision_id !== "string" || result.basis_id !== command.expected_basis_id ||
        result.target_id !== decodeURIComponent(path.split("/")[4]))) {
    throw new ApiError("invalid_command_receipt", "Исход неизвестен: ответ не соответствует отправленной команде", true);
  }
}

export async function request<T>(path: string, token: string, body?: string): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20_000);
  let response: Response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body,
      signal: controller.signal,
    });
  } catch {
    clearTimeout(timeout);
    throw new ApiError("network_error", "Ответ не получен; исход команды неизвестен", body !== undefined);
  }
  let value;
  try { value = await response.json(); }
  catch {
    throw new ApiError("invalid_response", "Невозможно прочитать ответ сервера", body !== undefined);
  } finally {
    clearTimeout(timeout);
  }
  if (!response.ok) {
    throw new ApiError(value?.error?.code ?? "http_error", value?.error?.message ?? `HTTP ${response.status}`,
      body !== undefined && response.status >= 500);
  }
  return value as T;
}

export const api = {
  session: (token: string) => request<components["schemas"]["Session"]>("/api/v2/session", token),
  scope: (token: string) => request<components["schemas"]["ScopeReview"]>("/api/v2/scope-constraints", token),
  items: async (token: string) => ({ items: await allPages<Item>("/api/v2/items", token), next_cursor: null }),
  item: (id: string, token: string) => request<Item>(`/api/v2/items/${encodeURIComponent(id)}`, token),
  basis: (id: string, token: string) => request<Basis>(`/api/v2/bases/${encodeURIComponent(id)}`, token),
  analytics: (token: string, query = "") => request<Analytics>(`/api/v2/analytics${query ? `?${query}` : ""}`, token),
  timeline: async (id: string, token: string) => ({ items: await allPages<components["schemas"]["TimelineEntry"]>(`/api/v2/items/${encodeURIComponent(id)}/timeline`, token), next_cursor: null }),
  orders: async (token: string) => ({ items: await allPages<components["schemas"]["WorkOrder"]>("/api/v2/work-orders", token), next_cursor: null }),
  line: (id: string, token: string) => request<components["schemas"]["LineStatus"]>(`/api/v2/lines/${encodeURIComponent(id)}/status`, token),
  deliveries: async (token: string) => {
    const items: components["schemas"]["Delivery"][] = [];
    let cursor: string | null = null;
    do {
      const page: components["schemas"]["DeliveryPage"] = await request(`/api/v2/integrations/deliveries?limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, token);
      items.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor);
    return items;
  },
};

async function allPages<T>(path: string, token: string): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  do {
    const page: { items: T[]; next_cursor: string | null } = await request(`${path}?limit=200${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, token);
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return items; // A stale timeline rejects the whole read, never merges mixed revisions.
}
