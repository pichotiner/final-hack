// Real Chromium + public HTTP acceptance on the isolated P6 stand.
import { chromium } from "@playwright/test";
import { readFile, writeFile, copyFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = new URL("../../", import.meta.url);
const dir = process.env.QC_DEMO_DIRECTORY;
const evidence = join(dir, "evidence");
const base = process.env.QC_API_URL;
const erp = process.env.QC_ERP_URL;
const ui = process.env.QC_UI_URL;
const tokens = JSON.parse(await readFile(join(dir, "credentials", "demo-tokens.json"), "utf8"));
const erpToken = (await readFile(join(dir, "credentials", "erp.token"), "utf8")).trim();
const profile = JSON.parse(await readFile(join(dir, "profile.json"), "utf8"));
const inputs = JSON.parse(await readFile(new URL("docs/acceptance/role2-m0-inputs.json", root), "utf8"));
const expected = JSON.parse(await readFile(new URL("docs/acceptance/role2-m0-expected.json", root), "utf8"));
const outcomes = [];
const trace = [];
const failures = [];
const save = async (name, value) => writeFile(join(evidence, name), JSON.stringify(value, null, 2) + "\n");
const assert = (condition, message) => { if (!condition) throw Error(message); };
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, part]) => [key, canonical(part)])) : value;
const eq = (actual, wanted, label) => assert(JSON.stringify(canonical(actual)) === JSON.stringify(canonical(wanted)), `${label}: ${JSON.stringify(actual)} != ${JSON.stringify(wanted)}`);
const expectation = id => expected.cases.find(c => c.id === id)?.expected;
for (const [id, item] of [["R2-NORMAL", "assembly-002"], ["R2-REWORK", "assembly-001"], ["R2-UNKNOWN", "assembly-003"]]) {
  if (inputs.cases.find(c => c.id === id)?.item !== item || !expectation(id)) throw Error(`Independent inputs/expectations missing: ${id}`);
}

async function http(origin, path, token, body, status = 200, headers = {}) {
  const response = await fetch(origin + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json();
  trace.push({ method: body === undefined ? "GET" : "POST", path: path.split("?")[0], status: response.status, request: body, response: value });
  assert(response.status === status, `${path}: HTTP ${response.status}, wanted ${status}: ${JSON.stringify(value)}`);
  return value;
}
async function get(path, token = tokens.controller) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try { return await http(base, path, token); }
    catch (error) { if (!(error instanceof TypeError) || attempt === 2) throw error; await new Promise(resolve => setTimeout(resolve, 200)); }
  }
}
const post = (path, body, token = tokens.controller, status = 201) => http(base, path, token, body, status);
const state = item => get(`/api/v2/items/${item}`);
const basis = id => get(`/api/v2/bases/${id}`);
const deliveryItems = async () => (await get("/api/v2/integrations/deliveries?limit=200", tokens.integration)).items;
const beforeCommand = async (item, target) => {
  const current = await state(item);
  const b = await basis(target ? current.nonconformities.find(c => c.id === target).basis_id : current.basis_id);
  return { expected_basis_id: b.basis_id, expected_revision: b.context_revision, reason: "P6 synthetic independent acceptance", idempotency_key: crypto.randomUUID() };
};

function doc(item, type, time, extra = {}, payload = {}) {
  const components = profile.items[item].component_mapping;
  return {
    event_id: crypto.randomUUID(), event_type: type, schema_version: "1.0",
    occurred_at: `2026-09-26T${time}Z`, source_id: type === "inspection_observed" ? "vision-emulator-1" : "line-emulator-1",
    item_id: item, item_type_id: "bracket-demo", line_id: "line-1", station_id: "station-assembly",
    operation_run_id: null, operator_id: null, equipment_id: null,
    identification_status: "reliable", time_quality: "synchronized",
    ...extra,
    payload: type === "item_received" ? { work_order_id: "order-001", component_ids: Object.values(components), assembly_version: "1", ...payload }
      : type.startsWith("operation_") ? { operation_type_id: "assembly", shift_id: "shift-1", ...payload }
      : payload,
  };
}
const ingest = async d => post("/api/v2/events", d, tokens[d.source_id === "vision-emulator-1" ? "vision-demo" : "line-demo"]);
async function operation(item, run, start, end, station = "assembly", previous = null) {
  const extra = { station_id: `station-${station}`, operation_run_id: run, operator_id: station === "machining" ? "operator-01" : "operator-02", equipment_id: `machine-${station === "machining" ? "01" : "02"}` };
  for (const [kind, time] of [["operation_started", start], ["operation_finished", end]])
    await ingest(doc(item, kind, time, extra, { operation_type_id: station, shift_id: "shift-1", ...(previous ? { previous_operation_run_id: previous } : {}) }));
}
async function inspection(item, point, time, { cycle = "cycle-1", session, run, result = "no_defect_signs", quality = "good", defects = [] } = {}) {
  const rule = profile.items[item].context.controls.find(c => c.checkpoint_id === point);
  const document = doc(item, "inspection_observed", time, { station_id: point === "after-machining" ? "station-machining" : "station-assembly", operation_run_id: run === undefined ? rule.operation_run_id ?? null : run }, {
    checkpoint_id: point, inspection_phase: rule.inspection_phase, inspection_result: result,
    observation_quality: quality, confidence: null, analyzer_version: "emulator-1", evidence_refs: [],
    control_cycle_id: cycle, control_session_id: session ?? rule.control_session_id, defects,
  });
  return { document, receipt: await ingest(document) };
}
async function seed(item, mode = "normal") {
  const context = profile.items[item].context;
  const [machiningRun, assemblyRun] = context.required_operations;
  const insert = Object.values(profile.items[item].component_mapping).find(value => value.includes("insert-"));
  await ingest(doc(item, "item_received", "10:01:00"));
  let incoming;
  if (mode === "incoming") incoming = await inspection(item, "incoming-surface", "10:02:00", { result: "defect_signs", defects: [{ defect_ref: "incoming-mark", type: "surface-mark", component_id: insert, region_id: "face-A" }] });
  else incoming = await inspection(item, "incoming-surface", "10:02:00");
  await operation(item, machiningRun, "10:03:00", "10:05:00", "machining");
  await inspection(item, "after-machining", "10:06:00");
  await operation(item, assemblyRun, "10:07:00", "10:09:00");
  if (mode === "warning") {
    await ingest(doc(item, "machine_state", "10:08:00", { operation_run_id: assemblyRun, equipment_id: "machine-02" }, { state: "warning", parameters: {}, description: "synthetic deviation" }));
    await ingest(doc(item, "operator_action", "10:08:10", { operation_run_id: assemblyRun, operator_id: "operator-02" }, { action_type: "adjust", details: { description: "synthetic response" } }));
  }
  const defect = { defect_ref: "mark-001", type: "surface-mark", component_id: insert, region_id: "face-A" };
  const result = ["defect", "warning", "two-signs", "poor-defect"].includes(mode) ? "defect_signs" : "no_defect_signs";
  const final = mode === "missing" ? null : await inspection(item, "final-surface", "10:10:00", {
    quality: mode === "poor" || mode === "poor-defect" ? "poor" : "good", result, defects: result === "defect_signs" ? mode === "two-signs" ? [defect, { ...defect, defect_ref: "mark-B", region_id: "face-B" }] : [defect] : [],
  });
  return { incoming, final, defect };
}

async function chapter(page, name) {
  await page.screenshot({ path: join(evidence, `${name}.png`) });
  if (Number(process.env.QC_SLOW_MS)) await page.waitForTimeout(Number(process.env.QC_SLOW_MS));
}
async function openItem(page, item, token = tokens.controller) {
  await page.getByLabel("Bearer token").fill(token);
  await page.getByRole("button", { name: "Загрузить изделия" }).click();
  await page.getByLabel(/^Изделие/).selectOption(item);
  await page.getByRole("heading", { name: new RegExp(item.replaceAll("-", "\\-")) }).waitFor();
}
async function uiCommand(page, item, action, { target = "", reason = "P6 synthetic independent acceptance", fields = {} } = {}) {
  await page.getByRole("button", { name: "Завершить просмотр и сменить цель" }).click();
  await page.getByLabel("Цель").selectOption(target);
  await page.getByRole("button", { name: "Начать / явно обновить рассмотрение" }).click();
  await page.getByText("B = ", { exact: false }).first().waitFor();
  await page.getByLabel("Действие").selectOption(action);
  await page.locator("textarea").first().fill(reason);
  for (const [label, value] of Object.entries(fields)) {
    const field = page.getByLabel(new RegExp(`^${label}`));
    if (await field.evaluate(e => e.tagName === "SELECT")) await field.selectOption(value);
    else await field.fill(value);
  }
  const path = action === "cycle" ? "/control-cycles" : action === "resolve" ? "/resolutions" : action === "confirm" ? "/decisions" : "/dispositions";
  const result = page.waitForResponse(response => response.url().includes(path) && response.request().method() === "POST");
  await page.getByRole("button", { name: "Отправить отдельную команду" }).click();
  const response = await result;
  const body = await response.json();
  assert(response.status() === 201, `UI ${action}: ${response.status()} ${JSON.stringify(body)}`);
  trace.push({ action: `UI ${action}`, item, target, request: JSON.parse(response.request().postData()), status: response.status(), response: body });
  await page.getByRole("status").getByText("Результат сервера:", { exact: false }).waitFor();
  return body;
}
async function worker(messageId) {
  const run = spawnSync(process.env.QC_PYTHON, ["-m", "app.worker", "--once", "--message-id", messageId], {
    cwd: new URL("../../backend/", import.meta.url), env: process.env, encoding: "utf8", timeout: 30000,
  });
  assert(run.status === 0, `worker ${messageId}: ${run.stderr}`);
  return run.stdout.trim();
}
async function caseRun(id, task) {
  try { const actual = await task(); outcomes.push({ case_id: id, status: "PASS", actual }); }
  catch (error) { const reason = String(error.stack ?? error); failures.push({ id, reason }); outcomes.push({ case_id: id, status: "FAIL", reason }); console.error(id, reason); await page.screenshot({ path: join(evidence, `failure-${outcomes.length}.png`) }); console.error((await page.locator("main").innerText()).slice(0, 1800)); }
}

const browser = await chromium.launch({ headless: process.env.QC_HEADED !== "1" });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, recordVideo: { dir: evidence, size: { width: 1440, height: 900 } } });
const page = await context.newPage();
page.on("dialog", dialog => dialog.accept());
try {
  await page.goto(ui);
  await caseRun("R2-NORMAL / UI + ACK", async () => {
    await seed("assembly-002");
    await openItem(page, "assembly-002");
    await page.getByRole("button", { name: "Задания ERP" }).click();
    await page.getByRole("table", { name: "Задания ERP" }).waitFor();
    assert((await page.locator("main").innerText()).includes("order-001"), "ERP order missing in UI");
    const released = await uiCommand(page, "assembly-002", "released");
    await chapter(page, "normal-release");
    const current = await state("assembly-002");
    eq(current.blockers, expectation("R2-NORMAL").blockers, "normal blockers");
    assert(current.release_allowed_now && released.decision_id, "normal release");
    const stats = (await get("/api/v2/analytics?item_id=assembly-002")).counts;
    for (const field of ["inspected_items", "confirmed_defects", "confirmed_nonconforming_items", "rework_operations"])
      eq(stats[field], expectation("R2-NORMAL")[field], `normal ${field}`);
    const history = (await basis(current.basis_id)).context_snapshot.production_history;
    const durations = history.operations.map(op => op.durations.find(d => d.kind === "station_elapsed" && d.origin === "calculated")?.value);
    eq(durations, expectation("R2-NORMAL").operation_station_elapsed_seconds, "two station elapsed durations");
    const pending = (await deliveryItems()).filter(x => x.item_id === "assembly-002");
    for (const message of pending) worker(message.message_id);
    const final = pending.find(x => x.result_version === Math.max(...pending.map(y => y.result_version)));
    const acknowledged = (await deliveryItems()).find(x => x.message_id === final.message_id);
    const saved = await http(erp, `/external/v2/quality-results/${final.message_id}`, erpToken);
    assert(acknowledged.status === "acknowledged" && saved.body.disposition_decision.decision_id === released.decision_id && saved.body.confirmed_nonconformities.length === 0, "normal release with decision and ACK");
    return { decision: released, item: current, counts: stats, station_elapsed_seconds: durations, ack: acknowledged.ack, erp_message_id: final.message_id };
  });
  await caseRun("A04 / R2-REWORK / UI + ACK", async () => {
    const item = "assembly-001";
    await seed(item, "defect");
    await openItem(page, item);
    await page.getByRole("button", { name: "История с обоими временами" }).click();
    await page.getByRole("table", { name: "История событий" }).getByText("operation_finished").first().waitFor();
    const first = await state(item);
    const card = first.nonconformities[0];
    assert(card && card.cause.status === "unresolved", "defect candidate / unresolved cause");
    const b1 = await basis(card.basis_id);
    const confirm = await uiCommand(page, item, "confirm", { target: card.id });
    const rework = await uiCommand(page, item, "rework");
    await openItem(page, item, tokens.master);
    const cycle = await uiCommand(page, item, "cycle", { fields: {
      "Решение о доработке": rework.decision_id,
      "Повторяемые операции": "run-assembly-001",
    } });
    const sessions = cycle.control_sessions;
    const bindings = cycle.checkpoint_operation_run_ids;
    const repairRun = bindings["final-surface"];
    assert(cycle.control_cycle_id !== first.control_cycle_id, "new server cycle");
    const beforeRepair = await state(item);
    const premature = await post(`/api/v2/nonconformities/${card.id}/resolutions`, {
      ...await beforeCommand(item, card.id), action: "resolve", rework_operation_run_id: repairRun,
      previous_operation_run_id: "run-assembly-001", control_record_ids: [b1.completeness[2].record_ids[0]],
    }, tokens.controller, 409);
    assert(premature.error.code && (await state(item)).decisions.length === beforeRepair.decisions.length, "A05 resolve before repair has no effect");
    await operation(item, repairRun, "10:20:00", "10:22:00", "assembly", "run-assembly-001");
    const records = [];
    for (const point of ["incoming-surface", "after-machining", "final-surface"]) {
      const { receipt } = await inspection(item, point, "10:23:00", { cycle: cycle.control_cycle_id, session: sessions[point], run: bindings[point] });
      records.push(receipt.record_id);
    }
    const earlyRelease = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "released" }, tokens.controller, 409);
    eq(earlyRelease.error.code, "release_blocked", "A05 release before resolve");
    await openItem(page, item);
    const resolve = await uiCommand(page, item, "resolve", { target: card.id, fields: {
      "Завершённая доработка": repairRun,
      "Записи повторного контроля": records[2],
    } });
    const release = await uiCommand(page, item, "released");
    await page.getByRole("button", { name: "Обновить доставки и ACK" }).isDisabled();
    const final = await state(item);
    eq(final.blockers, expectation("R2-REWORK").blockers, "A04 blockers");
    assert(final.nonconformities[0].resolution_status === "resolved", "resolved card retained");
    eq(await basis(b1.basis_id), b1, "old B immutable");
    const metrics = await get("/api/v2/analytics?item_id=assembly-001");
    for (const field of ["inspected_items", "confirmed_defects", "confirmed_nonconforming_items", "open_confirmed_defects", "resolved_confirmed_defects", "rework_operations", "confirmed_operator_errors"])
      eq(metrics.counts[field], expectation("R2-REWORK")[field], `A04 ${field}`);
    await chapter(page, "a04-release");
    const pending = { items: await deliveryItems() };
    const finalMessage = pending.items.find(x => x.item_id === item && x.result_version === Math.max(...pending.items.filter(y => y.item_id === item).map(y => y.result_version)));
    assert(finalMessage?.status === "pending", "final outbox pending before worker");
    const payload = finalMessage.payload;
    // Commit at ERP, then drop ACK; worker retries the original durable message.
    try { await http(erp, "/external/v2/quality-results", erpToken, payload, 201, { "X-Emulator-Fault": "lost-ack" }); }
    catch (e) { assert(e instanceof TypeError, `expected lost transport ACK: ${e}`); }
    worker(finalMessage.message_id);
    const acknowledged = (await deliveryItems()).find(x => x.message_id === finalMessage.message_id);
    assert(acknowledged.status === "acknowledged" && acknowledged.ack.status === "duplicate", "original ERP ACK verified");
    for (const message of pending.items.filter(x => x.item_id === item && x.message_id !== finalMessage.message_id)) worker(message.message_id);
    const saved = await http(erp, `/external/v2/quality-results/${finalMessage.message_id}`, erpToken);
    const stream = Object.fromEntries(["producer_id", "external_system", "external_order_id", "external_item_id"].map(key => [key, payload[key]]));
    const currentErp = await http(erp, `/external/v2/quality-state?${new URLSearchParams(stream)}`, erpToken);
    eq(saved.body, payload, "saved ERP body");
    eq(currentErp.current, saved, "ERP current released");
    eq(currentErp.missing_version_ranges, [], "ERP no missing versions");
    const olderSaved = await http(erp, `/external/v2/quality-results/${pending.items.find(x => x.item_id === item && x.result_version === 1).message_id}`, erpToken);
    eq(olderSaved.ack.application_status, "superseded", "A09 older version delivered after newer");
    eq(saved.ack.application_status, "applied", "A09 V2 remains current");
    assert(saved.body.disposition_decision.decision_id === release.decision_id, "release basis in ERP");
    await page.getByLabel("Токен интеграции").fill(tokens.integration);
    await page.getByRole("button", { name: "Обновить доставки и ACK" }).click();
    await page.getByRole("table", { name: "Доставки ERP" }).getByText(finalMessage.message_id).waitFor();
    await page.getByRole("table", { name: "Доставки ERP" }).getByText("acknowledged").first().waitFor();
    await page.getByRole("button", { name: "Завершить просмотр и сменить цель" }).click();
    await page.getByRole("table", { name: "История решений" }).getByText("released").waitFor();
    await page.getByRole("button", { name: b1.basis_id }).first().click();
    await page.getByText(`Сохранённое основание решения ${b1.basis_id}`).click();
    assert(await page.getByText(`Сохранённое основание решения ${b1.basis_id}`).locator("xpath=..").getAttribute("open") !== null, "decision B visible in browser");
    await chapter(page, "a04-ack-basis");
    await save("a04.json", { first: b1, confirm, rework, cycle, records, premature, earlyRelease, resolve, release, final, metrics, saved, acknowledged, currentErp, olderSaved });
    return { item, metrics: metrics.counts, decision_ids: [confirm.decision_id, rework.decision_id, resolve.decision_id, release.decision_id], result_version: payload.result_version, ack_status: acknowledged.ack.status, historical_status: olderSaved.ack.application_status };
  });
  await caseRun("R2-UNKNOWN / poor + negative", async () => {
    const item = "assembly-003";
    const { final } = await seed(item, "poor");
    const current = await state(item);
    const b = await basis(current.basis_id);
    const observed = b.analysis_snapshot.observations.find(o => o.record_id === final.receipt.record_id);
    eq(observed.result, expectation("R2-UNKNOWN").raw_final_result, "raw negative");
    eq(observed.effective_result, expectation("R2-UNKNOWN").final_effective_result, "effective unknown");
    assert(current.blockers.some(x => x.startsWith("not_assessable:final-surface")), "poor control blocks release");
    const attempt = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "released" }, tokens.controller, 409);
    assert(attempt.error.code === "release_blocked", "release denied");
    eq((await state(item)).decisions.length, expectation("R2-UNKNOWN").new_release_decisions, "no release decision");
    await openItem(page, item);
    await chapter(page, "unknown-quality");
    return { raw_record_id: final.receipt.record_id, raw_result: observed.result, effective_result: observed.effective_result, blockers: current.blockers, denial: attempt };
  });
  await caseRun("A01/A02 / late after released, stale and historical B", async () => {
    const item = "assembly-002";
    const old = await state(item);
    const b1 = await basis(old.basis_id);
    const oldDecision = old.decisions.find(d => d.action === "released");
    const prepared = { ...await beforeCommand(item), disposition: "released" };
    const late = await inspection(item, "incoming-surface", "10:01:30", {
      result: "defect_signs", defects: [{ defect_ref: "late-mark", type: "surface-mark", component_id: "insert-002", region_id: "face-A" }],
    });
    const updated = await state(item);
    const b2 = await basis(updated.basis_id);
    eq(await basis(b1.basis_id), b1, "old B unchanged after late");
    assert(!b1.inputs.some(x => x.record_id === late.receipt.record_id) && b2.inputs.some(x => x.record_id === late.receipt.record_id), "late only in B2");
    assert(updated.decisions.some(d => d.decision_id === oldDecision.decision_id) && !updated.release_allowed_now, "historical release retained, current review blocked");
    const count = updated.decisions.length;
    const rejected = await post(`/api/v2/items/${item}/dispositions`, prepared, tokens.controller, 409);
    assert(rejected.error.code === "stale_revision" && (await state(item)).decisions.length === count, "stale has no decision effect");
    await openItem(page, item);
    await chapter(page, "late-then-now");
    return { old_basis_id: b1.basis_id, new_basis_id: b2.basis_id, old_decision_id: oldDecision.decision_id, late_record_id: late.receipt.record_id, blockers: updated.blockers, denied: rejected.error.code };
  });
  await caseRun("R2-DUPLICATE / A08 transport", async () => {
    const item = "duplicate-assembly-001";
    const { final } = await seed(item, "defect");
    const before = await state(item);
    const first = await basis(before.basis_id);
    const outboxBefore = (await deliveryItems()).filter(x => x.item_id === item).length;
    const metricsBefore = (await get(`/api/v2/analytics?item_id=${item}`)).counts;
    const replay = await post("/api/v2/events", final.document, tokens["vision-demo"], 200);
    const after = await state(item);
    eq(replay.record_id, final.receipt.record_id, "duplicate receipt record");
    eq(replay.received_at, final.receipt.received_at, "original time");
    eq(after.context_revision, before.context_revision, "no revision on duplicate");
    eq(await basis(after.basis_id), first, "no B change");
    eq((await deliveryItems()).filter(x => x.item_id === item).length, outboxBefore, "no extra outbox on duplicate");
    eq((await get(`/api/v2/analytics?item_id=${item}`)).counts, metricsBefore, "no KPI change on duplicate");
    return { first: final.receipt, duplicate: replay, revision: after.context_revision };
  });
  await caseRun("R2-SECOND-OBSERVATION / same logical defect", async () => {
    const item = "second-assembly-001";
    const { final } = await seed(item, "defect");
    const again = structuredClone(final.document);
    again.event_id = crypto.randomUUID(); again.occurred_at = "2026-09-26T10:11:00Z";
    const receipt = await ingest(again);
    const current = await state(item);
    const b = await basis(current.basis_id);
    assert(current.nonconformities.length === expectation("R2-SECOND-OBSERVATION").logical_candidates_before_confirm, "one candidate");
    const signs = b.analysis_snapshot.observations.filter(o => o.defect_ref === "mark-001");
    eq(signs.length, expectation("R2-SECOND-OBSERVATION").positive_observations_for_candidate, "two signs");
    await post(`/api/v2/nonconformities/${current.nonconformities[0].id}/decisions`, { ...await beforeCommand(item, current.nonconformities[0].id), decision: "confirm" });
    const analytics = await get(`/api/v2/analytics?item_id=${item}`);
    eq(analytics.counts.confirmed_defects, expectation("R2-SECOND-OBSERVATION").confirmed_defects_after_confirm, "one confirmed defect");
    return { first: final.receipt.record_id, second: receipt.record_id, observations: signs.map(x => x.observation_id), confirmed: analytics.counts.confirmed_defects };
  });
  await caseRun("R2-TWO-SIGNS / distinct indices and cards", async () => {
    const item = "two-signs-assembly-001";
    const { final } = await seed(item, "two-signs");
    const first = await state(item);
    const b = await basis(first.basis_id);
    const signs = b.analysis_snapshot.observations.filter(x => x.record_id === final.receipt.record_id);
    eq(signs.length, expectation("R2-TWO-SIGNS").positive_observations, "two observations");
    eq(new Set(signs.map(x => x.defect_index)).size, expectation("R2-TWO-SIGNS").distinct_feature_indices, "two feature indices");
    eq(first.nonconformities.length, expectation("R2-TWO-SIGNS").logical_candidates, "two cards");
    for (const card of first.nonconformities)
      await post(`/api/v2/nonconformities/${card.id}/decisions`, { ...await beforeCommand(item, card.id), decision: "confirm" });
    const analytics = await get(`/api/v2/analytics?item_id=${item}`);
    eq(analytics.counts.confirmed_defects, expectation("R2-TWO-SIGNS").confirmed_defects_after_both_confirmations, "two confirmed signs");
    return { record_id: final.receipt.record_id, signs: signs.map(x => ({ observation_id: x.observation_id, defect_index: x.defect_index, region_id: x.region_id })), confirmed: analytics.counts.confirmed_defects };
  });
  await caseRun("R2-WARNING / station, operator, cause unknown", async () => {
    const item = "warning-assembly-001";
    await seed(item, "warning");
    const current = await state(item);
    const b = await basis(current.basis_id);
    const history = await get(`/api/v2/items/${item}/timeline`);
    assert(history.items.some(x => x.event_type === "machine_state") && history.items.some(x => x.event_type === "operator_action"), "machine and action in timeline");
    eq(current.nonconformities[0].cause.status, "unresolved", "no automatic established cause");
    const analytics = await get(`/api/v2/analytics?item_id=${item}`);
    eq(analytics.counts.confirmed_operator_errors, expectation("R2-WARNING").confirmed_operator_errors, "no automatic operator blame");
    return { basis_id: b.basis_id, timeline_types: history.items.map(x => x.event_type), cause: current.nonconformities[0].cause, operator_errors: analytics.counts.confirmed_operator_errors };
  });
  await caseRun("incoming defect / no operator attribution", async () => {
    const item = "incoming-assembly-001";
    await seed(item, "incoming");
    const current = await state(item);
    const b = await basis(current.basis_id);
    assert(current.nonconformities.length === 1 && b.analysis_snapshot.observations.some(x => x.checkpoint_id === "incoming-surface" && x.defect_ref === "incoming-mark"), "incoming sign retained");
    const analytics = await get(`/api/v2/analytics?item_id=${item}`);
    eq(analytics.counts.confirmed_operator_errors, 0, "incoming event cannot blame operator");
    return { card: current.nonconformities[0], operator_errors: analytics.counts.confirmed_operator_errors };
  });
  await caseRun("missing control / release blocked", async () => {
    const item = "missing-assembly-001";
    await seed(item, "missing");
    const current = await state(item);
    assert(current.blockers.includes("missing_checkpoint:final-surface"), "final checkpoint missing");
    const denial = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "released" }, tokens.controller, 409);
    eq(denial.error.code, "release_blocked", "missing cannot release");
    return { blockers: current.blockers, denied: denial.error.code };
  });
  await caseRun("A07 / contradictory control and distinct blockers", async () => {
    const item = "disputed-assembly-001";
    await seed(item, "normal");
    const negative = await state(item);
    await inspection(item, "final-surface", "10:10:30", { result: "defect_signs", defects: [{ defect_ref: "disputed-mark", type: "surface-mark", component_id: "disputed-insert-001", region_id: "face-A" }] });
    const current = await state(item);
    assert(current.context_revision > negative.context_revision && current.blockers.some(x => x.startsWith("disputed_checkpoint:final-surface")), "conflicting observations are disputed");
    const denial = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "released" }, tokens.controller, 409);
    eq(denial.error.code, "release_blocked", "disputed cannot release");
    return { before_revision: negative.context_revision, after_revision: current.context_revision, blockers: current.blockers, denial: denial.error.code };
  });
  await caseRun("A07 / rejecting poor sign cannot turn control good", async () => {
    const item = "a05-before-repair-assembly-001";
    await seed(item, "poor-defect");
    const current = await state(item);
    assert(current.nonconformities.length === 1 && current.blockers.includes("not_assessable:final-surface"), "poor sign needs check");
    const rejected = await post(`/api/v2/nonconformities/${current.nonconformities[0].id}/decisions`, { ...await beforeCommand(item, current.nonconformities[0].id), decision: "reject" });
    const after = await state(item);
    const denial = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "released" }, tokens.controller, 409);
    assert(rejected.decision_id && after.blockers.includes("not_assessable:final-surface") && denial.error.code === "release_blocked", "reject must not upgrade observation quality");
    return { rejection: rejected.decision_id, blockers: after.blockers, denial: denial.error.code };
  });
  await caseRun("A05 / no repeat controls after completed repair", async () => {
    const item = "a05-no-control-assembly-001";
    const { final } = await seed(item, "defect");
    const current = await state(item);
    const card = current.nonconformities[0];
    await post(`/api/v2/nonconformities/${card.id}/decisions`, { ...await beforeCommand(item, card.id), decision: "confirm" });
    const rework = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "rework" });
    const sessions = Object.fromEntries(profile.items[item].context.controls.map(c => [c.checkpoint_id, `repeat-${c.checkpoint_id}`]));
    const cycle = await post(`/api/v2/items/${item}/control-cycles`, { ...await beforeCommand(item), rework_disposition_id: rework.decision_id, planned_operation_run_ids: ["no-control-repair"], control_sessions: sessions, checkpoint_operation_run_ids: { "incoming-surface": null, "after-machining": "no-control-repair", "final-surface": "no-control-repair" } }, tokens.master);
    await operation(item, "no-control-repair", "10:20:00", "10:22:00", "assembly", "a05-no-control-run-assembly-001");
    const before = await state(item);
    const outbox = (await deliveryItems()).filter(x => x.item_id === item).length;
    const denial = await post(`/api/v2/nonconformities/${card.id}/resolutions`, { ...await beforeCommand(item, card.id), action: "resolve", rework_operation_run_id: "no-control-repair", previous_operation_run_id: "a05-no-control-run-assembly-001", control_record_ids: [final.receipt.record_id] }, tokens.controller, 409);
    const after = await state(item);
    assert(denial.error.code && after.decisions.length === before.decisions.length && (await deliveryItems()).filter(x => x.item_id === item).length === outbox, "failed resolution is atomic");
    assert(after.blockers.includes("missing_checkpoint:final-surface"), "no old control carried into new cycle");
    return { cycle: cycle.control_cycle_id, error: denial.error.code, blockers: after.blockers, extra_decisions: 0, extra_messages: 0 };
  });
  await caseRun("R1-02 / A06 old session, foreign run, wrong cycle", async () => {
    const item = "a06-assembly-001";
    await seed(item, "defect");
    const initial = await state(item);
    const card = initial.nonconformities[0];
    await post(`/api/v2/nonconformities/${card.id}/decisions`, { ...await beforeCommand(item, card.id), decision: "confirm" });
    const rework = await post(`/api/v2/items/${item}/dispositions`, { ...await beforeCommand(item), disposition: "rework" });
    const sessions = Object.fromEntries(profile.items[item].context.controls.map(c => [c.checkpoint_id, `new-${c.checkpoint_id}`]));
    const runs = { "incoming-surface": null, "after-machining": "a06-repair", "final-surface": "a06-repair" };
    const cycle = await post(`/api/v2/items/${item}/control-cycles`, { ...await beforeCommand(item), rework_disposition_id: rework.decision_id, planned_operation_run_ids: ["a06-repair"], control_sessions: sessions, checkpoint_operation_run_ids: runs }, tokens.master);
    await operation(item, "a06-repair", "10:20:00", "10:22:00", "assembly", "a06-run-assembly-001");
    await inspection(item, "incoming-surface", "10:23:00", { cycle: cycle.control_cycle_id, session: profile.items[item].context.controls[0].control_session_id, run: null });
    await inspection(item, "after-machining", "10:23:10", { cycle: cycle.control_cycle_id, session: sessions["after-machining"], run: "foreign-run" });
    await inspection(item, "final-surface", "10:23:20", { cycle: initial.control_cycle_id, session: sessions["final-surface"], run: "a06-repair" });
    const current = await state(item);
    for (const checkpoint of ["incoming-surface", "after-machining", "final-surface"])
      assert(current.blockers.includes(`missing_checkpoint:${checkpoint}`), `bad binding cannot close ${checkpoint}`);
    assert(current.blockers.includes("observation_cycle_unresolved"), "unresolved claims remain fail closed");
    return { cycle: cycle.control_cycle_id, blockers: current.blockers, full_A06: "BLOCKED: no carry endpoint" };
  });
  await caseRun("A03 / browser lost response, 403, exact retry", async () => {
    const item = "retry-ui-assembly-001";
    await seed(item);
    await openItem(page, item);
    const sent = [];
    let n = 0;
    const route = async intercepted => {
      const body = intercepted.request().postData();
      sent.push(body);
      n++;
      if (n === 1) { await intercepted.fetch(); await intercepted.abort("failed"); }
      else if (n === 2) await intercepted.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ error: { code: "permission_denied", message: "revoked", details: [] }, request_id: "isolated-p6" }) });
      else await intercepted.continue();
    };
    await page.route(`**/api/v2/items/${item}/dispositions`, route);
    try {
      await page.getByRole("button", { name: "Начать / явно обновить рассмотрение" }).click();
      await page.getByText("B = ", { exact: false }).first().waitFor();
      await page.getByLabel("Действие").selectOption("released");
      await page.locator("textarea").first().fill("P6 lost response test");
      await page.getByRole("button", { name: "Отправить отдельную команду" }).click();
      const retry = page.getByRole("button", { name: "Исход неизвестен: повторить точное тело и ключ" });
      await retry.waitFor();
      const first = await state(item);
      assert(first.decisions.length === 1, "server committed before response loss");
      await retry.click();
      await page.getByRole("status").getByText("revoked").waitFor();
      assert(await retry.isVisible(), "403 must retain unknown outcome and saved body");
      await chapter(page, "unknown-outcome-403");
      await retry.click();
      await page.getByRole("status").getByText("Результат сервера:", { exact: false }).waitFor();
      assert(sent.length === 3 && sent.every(x => x === sent[0]), "every UI attempt must send exact same body/key");
      const after = await state(item);
      eq(after.context_revision, first.context_revision, "retry does not bump revision");
      eq(after.decisions.length, 1, "retry does not add decisions");
      return { attempts: sent.length, same_body: true, decision_id: after.decisions[0].decision_id, revision: after.context_revision };
    } finally { await page.unroute(`**/api/v2/items/${item}/dispositions`, route); }
  });
  await caseRun("A12 / source cannot decide, wrong basis", async () => {
    const item = "a12-assembly-001";
    await seed(item, "defect");
    const current = await state(item);
    const card = current.nonconformities[0];
    const valid = { ...await beforeCommand(item, card.id), decision: "confirm" };
    const path = `/api/v2/nonconformities/${card.id}/decisions`;
    const source = await post(path, valid, tokens["vision-demo"], 403);
    const wrong = await post(path, { ...valid, expected_basis_id: (await state("assembly-003")).basis_id, idempotency_key: crypto.randomUUID() }, tokens.controller, 409);
    assert(source.error.code && wrong.error.code && (await state(item)).decisions.length === 0, "role and basis errors do not commit");
    return { role_denial: source.error.code, basis_denial: wrong.error.code, decisions_after: 0 };
  });
  await caseRun("A10 / ERP identity conflicts", async () => {
    const erpSaved = (await readFile(join(evidence, "a04.json"), "utf8"));
    const payload = JSON.parse(erpSaved).saved.body;
    const clash = await http(erp, "/external/v2/quality-results", erpToken, { ...payload, disposition_decision: { ...payload.disposition_decision, reason: "changed valid reason" } }, 409);
    const version = await http(erp, "/external/v2/quality-results", erpToken, { ...payload, message_id: crypto.randomUUID() }, 409);
    eq(clash.error.code, "message_id_conflict", "same ID different body");
    eq(version.error.code, "result_version_conflict", "same version different ID");
    return { same_message_changed_body: clash.error.code, same_version_new_message: version.error.code };
  });
  await caseRun("A09 / V2-V1-V2 order at persistent ERP", async () => {
    const item = "disputed-assembly-001";
    const messages = (await deliveryItems()).filter(x => x.item_id === item).sort((a, b) => a.result_version - b.result_version);
    const [v1, v2] = messages;
    assert(v1?.result_version === 1 && v2?.result_version === 2, "isolated real outbox versions");
    const first = await http(erp, "/external/v2/quality-results", erpToken, v2.payload, 201);
    const older = await http(erp, "/external/v2/quality-results", erpToken, v1.payload, 201);
    const duplicate = await http(erp, "/external/v2/quality-results", erpToken, v2.payload, 200);
    const keys = Object.fromEntries(["producer_id", "external_system", "external_order_id", "external_item_id"].map(key => [key, v2.payload[key]]));
    const current = await http(erp, `/external/v2/quality-state?${new URLSearchParams(keys)}`, erpToken);
    eq([first.application_status, older.application_status, duplicate.status], ["applied", "superseded", "duplicate"], "V2 V1 V2 statuses");
    eq(current.current.body.message_id, v2.message_id, "current V2");
    return { first, older, duplicate, current_message_id: current.current.body.message_id };
  });
} finally {
  await context.close();
  await save("http-trace.json", trace);
  await save("results.json", { base: "P5 39c332a + working tree", independent_expectations: "docs/acceptance/role2-m0-expected.json", outcomes, failures });
  if (process.env.QC_PUBLISH_DEMO === "1" && failures.length === 0) {
    const publication = new URL("docs/demo/", root);
    const summary = outcomes.map(({ case_id, status, actual }) => ({ case_id, status,
      actual: case_id.startsWith("R2-NORMAL") ? { decision_id: actual.decision.decision_id, erp_message_id: actual.erp_message_id, ack_status: actual.ack.status } : actual,
    }));
    await writeFile(new URL("results-summary.json", publication), JSON.stringify({ synthetic: true, environment: "environment.json", independent_expectations: "docs/acceptance/role2-m0-expected.json", cases: summary }, null, 2) + "\n");
    for (const name of ["environment.json", "a04.json", "import.jsonl"])
      await copyFile(join(evidence, name), new URL(name, publication));
    for (const name of ["normal-release.png", "a04-release.png", "a04-ack-basis.png", "unknown-quality.png", "late-then-now.png", "unknown-outcome-403.png"])
      await copyFile(join(evidence, name), new URL(name, publication));
    await page.video().saveAs(fileURLToPath(new URL("walkthrough.webm", publication)));
  }
  await browser.close();
}
if (failures.length) process.exitCode = 1;
