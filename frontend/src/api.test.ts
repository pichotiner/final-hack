import { afterEach, expect, it, vi } from "vitest";
import { request, verifyCommandReceipt } from "./api";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it("a success-shaped response for another target/basis is not a definitive receipt", () => {
  const body = JSON.stringify({ expected_basis_id: "b5" });
  expect(() => verifyCommandReceipt("/api/v2/items/a/dispositions", body,
    { decision_id: "d1", context_revision: 6, basis_id: "b5", target_id: "other" })).toThrow();
  expect(() => verifyCommandReceipt("/api/v2/items/a/dispositions", body,
    { decision_id: "d1", context_revision: 6, basis_id: "b6", target_id: "a" })).toThrow();
  verifyCommandReceipt("/api/v2/items/a/dispositions", body,
    { decision_id: "d1", context_revision: 6, basis_id: "b5", target_id: "a" });
});

it("a timed-out POST aborts but remains an unknown outcome", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn((_path, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new Error("aborted")));
  })));
  const check = expect(request("/api/v2/items/a/dispositions", "token", "{}"))
    .rejects.toMatchObject({ outcomeUnknown: true });
  await vi.advanceTimersByTimeAsync(20_000);
  await check;
});

it("timeout is an unknown command outcome, not a safe invitation to change its key", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("network")));
  await expect(request("/api/v2/items/a/dispositions", "token", "{}"))
    .rejects.toMatchObject({ outcomeUnknown: true, code: "network_error" });
});

it("stale rejection is definitive and the request transmits exact saved bytes", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "stale_revision", message: "stale" } }), { status: 409 }));
  vi.stubGlobal("fetch", fetch);
  const body = '{"expected_revision":5,"idempotency_key":"same"}';
  await expect(request("/api/v2/items/a/dispositions", "token", body))
    .rejects.toMatchObject({ outcomeUnknown: false, code: "stale_revision" });
  expect(fetch.mock.calls[0][1].body).toBe(body);
});
