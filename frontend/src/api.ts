import type { components } from "./generated/api";

export type Item = components["schemas"]["Item"];
export type WorkOrder = components["schemas"]["WorkOrder"];
export type Nonconformity = components["schemas"]["Nonconformity"];
export type Analytics = components["schemas"]["Analytics"];
export type TimelineEntry = components["schemas"]["TimelineEntry"];
export type LineStatus = components["schemas"]["LineStatus"];
export type Delivery = components["schemas"]["Delivery"];
export type DecisionCommand = components["schemas"]["DecisionCommand"];
export type DispositionCommand = components["schemas"]["DispositionCommand"];

const base = import.meta.env.VITE_API_BASE_URL || "";

export class ApiFailure extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export async function request<T>(
  path: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, { ...init, headers });
  } catch {
    throw new ApiFailure(
      0,
      "network_error",
      "Нет связи с API. Проверьте адрес и состояние backend.",
    );
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiFailure(
      response.status,
      body?.error?.code || "http_error",
      body?.error?.message || `HTTP ${response.status}`,
    );
  }
  return response.json() as Promise<T>;
}

export const api = {
  workOrders: (token: string) =>
    request<{ items: WorkOrder[]; next_cursor: string | null }>(
      "/api/v1/work-orders",
      token,
    ),
  items: (token: string) =>
    request<{ items: Item[]; next_cursor: string | null }>(
      "/api/v1/items",
      token,
    ),
  item: (id: string, token: string) =>
    request<Item>(`/api/v1/items/${encodeURIComponent(id)}`, token),
  timeline: (id: string, token: string) =>
    request<{ items: TimelineEntry[]; next_cursor: string | null }>(
      `/api/v1/items/${encodeURIComponent(id)}/timeline`,
      token,
    ),
  nonconformities: (token: string) =>
    request<{ items: Nonconformity[]; next_cursor: string | null }>(
      "/api/v1/nonconformities",
      token,
    ),
  analytics: (token: string) => request<Analytics>("/api/v1/analytics", token),
  line: (id: string, token: string) =>
    request<LineStatus>(
      `/api/v1/lines/${encodeURIComponent(id)}/status`,
      token,
    ),
  deliveries: (token: string) =>
    request<{ items: Delivery[]; next_cursor: string | null }>(
      "/api/v1/integrations/deliveries",
      token,
    ),
  decide: (id: string, body: DecisionCommand, token: string) =>
    request(
      `/api/v1/nonconformities/${encodeURIComponent(id)}/decisions`,
      token,
      { method: "POST", body: JSON.stringify(body) },
    ),
  disposition: (id: string, body: DispositionCommand, token: string) =>
    request(`/api/v1/items/${encodeURIComponent(id)}/dispositions`, token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
