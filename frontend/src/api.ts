import type { components } from "./generated/api";

export type Item = components["schemas"]["Item"];
export type Basis = components["schemas"]["Basis"];
export type Card = components["schemas"]["Nonconformity"];
export type ReviewCommand = components["schemas"]["ReviewCommand"];
export type DispositionCommand = components["schemas"]["DispositionCommand"];
export type ResolutionCommand = components["schemas"]["ResolutionCommand"];
export type CycleCommand = components["schemas"]["CycleCommand"];
export type Analytics = components["schemas"]["Analytics"];

export class ApiError extends Error {
  constructor(public code: string, message: string, public outcomeUnknown = false) {
    super(message);
  }
}

export function verifyCommandReceipt(path: string, body: string, result: Record<string, unknown>) {
  const command = JSON.parse(body);
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
  line: (id: string, token: string) => request<components["schemas"]["LineStatus"]>(`/api/v2/lines/${encodeURIComponent(id)}/status`, token),
  deliveries: (token: string) => request<components["schemas"]["DeliveryPage"]>("/api/v2/integrations/deliveries", token),
  items: (token: string) => request<components["schemas"]["ItemPage"]>("/api/v2/items", token),
  item: (id: string, token: string) => request<Item>(`/api/v2/items/${encodeURIComponent(id)}`, token),
  basis: (id: string, token: string) => request<Basis>(`/api/v2/bases/${encodeURIComponent(id)}`, token),
  analytics: (token: string) => request<Analytics>("/api/v2/analytics", token),
  timeline: (id: string, token: string) => request<components["schemas"]["TimelinePage"]>(`/api/v2/items/${encodeURIComponent(id)}/timeline`, token),
};
