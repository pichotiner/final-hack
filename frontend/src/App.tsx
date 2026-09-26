import { useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError, request, verifyCommandReceipt, type Basis, type Item, type ReviewCommand, type DispositionCommand, type ResolutionCommand, type CycleCommand } from "./api";
import { basisDiff, initialState, reviewReducer, type SavedCommand } from "./review-state";

const ids = (value: string) => value.split(",").map(x => x.trim()).filter(Boolean);

export default function App() {
  const [token, setToken] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [selected, setSelected] = useState("");
  const [latest, setLatest] = useState<Item | null>(null);
  const [history, setHistory] = useState<Awaited<ReturnType<typeof api.timeline>> | null>(null);
  const [message, setMessage] = useState("Введите bearer token и загрузите изделия из API. Контракт 0.4.0 / P5.");
  const [review, dispatch] = useReducer(reviewReducer, initialState);
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState("confirm");
  const [target, setTarget] = useState("");
  const [run, setRun] = useState("");
  const [previousRun, setPreviousRun] = useState("");
  const [controlRecords, setControlRecords] = useState("");
  const [reworkDecision, setReworkDecision] = useState("");
  const [sessions, setSessions] = useState("{}");
  const [bindings, setBindings] = useState("{}");
  const inFlight = useRef(false);
  const [oldBasis, setOldBasis] = useState<Basis | null>(null);
  const [oldBasisId, setOldBasisId] = useState("");
  const [newBasis, setNewBasis] = useState<Basis | null>(null);
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const locked = review.phase === "sending" || review.phase === "unknown";
  const pinned = review.pinned;
  const stale = pinned !== null && review.latestRevision !== pinned.item.context_revision;

  const error = (value: unknown) => setMessage(value instanceof Error ? value.message : String(value));
  async function refresh() {
    try { setItems((await api.items(token)).items); } catch (e) { error(e); }
  }
  useEffect(() => {
    if (!selected || !token) return;
    let active = true;
    async function poll() {
      try {
        const value = await api.item(selected, token);
        if (!active) return;
        setLatest(value); dispatch({ type: "poll", revision: value.context_revision });
      } catch (e) { if (active) error(e); }
    }
    void poll();
    const timer = setInterval(() => void poll(), 5000);
    return () => { active = false; clearInterval(timer); };
  }, [selected, token]);
  useEffect(() => {
    if (!locked) return;
    const protect = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [locked]);

  async function pin() {
    try {
      if (reason.trim() && pinned && !window.confirm("Перенести черновик причины на явно обновлённые основания?")) setReason("");
      const item = await api.item(selected, token);
      const card = target ? item.nonconformities.find(x => x.id === target) : null;
      if (target && !card) throw new Error("Карточка не принадлежит выбранному изделию");
      const basis = await api.basis(card ? card.basis_id : item.basis_id, token);
      dispatch({ type: "pin", value: { item, basis, targetId: target || null } });
      setLatest(item); setNewBasis(basis);
      setMessage(`Закреплено основание ${basis.basis_id}, ревизия ${basis.context_revision}`);
    } catch (e) { error(e); }
  }

  async function transmit(command: SavedCommand) {
    try {
      const result = await request<Record<string, unknown>>(command.path, token, command.body);
      verifyCommandReceipt(command.path, command.body, result);
      dispatch({ type: "success" });
      setMessage(`Результат сервера: ${JSON.stringify(result)}. Для следующей команды откройте новое рассмотрение.`);
    } catch (e) {
      dispatch({ type: e instanceof ApiError && e.outcomeUnknown ? "unknown" : "rejected" });
      error(e);
    } finally { inFlight.current = false; }
  }

  useEffect(() => {
    if (review.phase !== "sending" || !review.pending || inFlight.current) return;
    inFlight.current = true;
    void transmit(review.pending);
  }, [review.phase, review.pending]);

  function send(command: SavedCommand, retry = false) {
    dispatch(retry ? { type: "retry" } : { type: "prepare", command });
  }

  function submit() {
    if (!pinned || locked || stale || review.phase !== "idle") return;
    const common = { reason: reason.trim(), expected_revision: pinned.basis.context_revision,
      expected_basis_id: pinned.basis.basis_id, idempotency_key: crypto.randomUUID() };
    if (!common.reason) return setMessage("Укажите основание решения");
    const cardPath = `/api/v2/nonconformities/${encodeURIComponent(pinned.targetId ?? "")}`;
    const itemPath = `/api/v2/items/${encodeURIComponent(pinned.item.item_id)}`;
    let path: string;
    let body: ReviewCommand | DispositionCommand | ResolutionCommand | CycleCommand;
    if (["confirm", "reject", "request_check"].includes(kind)) {
      if (!pinned.targetId) return setMessage("Выберите карточку и начните её рассмотрение");
      body = { ...common, decision: kind as ReviewCommand["decision"] }; path = `${cardPath}/decisions`;
    } else if (kind === "resolve" || kind === "reopen") {
      if (!pinned.targetId) return setMessage("Выберите карточку");
      body = kind === "reopen" ? { ...common, action: "reopen" } : {
        ...common, action: "resolve", rework_operation_run_id: run,
        previous_operation_run_id: previousRun, control_record_ids: ids(controlRecords),
      };
      if (kind === "resolve" && (!run || !previousRun || !ids(controlRecords).length)) return setMessage("Нужны run доработки, предыдущий run и записи повторного контроля");
      path = `${cardPath}/resolutions`;
    } else {
      if (pinned.targetId !== null) return setMessage("Откройте рассмотрение изделия, не карточки");
      if (kind === "cycle") {
        if (!reworkDecision || !ids(run).length) return setMessage("Нужны решение rework и запланированные run ID");
        let control_sessions: Record<string, string>;
        let checkpoint_operation_run_ids: Record<string, string | null>;
        try {
          control_sessions = JSON.parse(sessions);
          checkpoint_operation_run_ids = JSON.parse(bindings);
          if (!control_sessions || !checkpoint_operation_run_ids || Array.isArray(control_sessions) || Array.isArray(checkpoint_operation_run_ids)) throw new Error();
        } catch { return setMessage("Укажите JSON-объекты привязок каждой контрольной точки"); }
        body = { ...common, rework_disposition_id: reworkDecision, planned_operation_run_ids: ids(run), control_sessions, checkpoint_operation_run_ids };
        path = `${itemPath}/control-cycles`;
      } else {
        body = { ...common, disposition: kind as DispositionCommand["disposition"] };
        path = `${itemPath}/dispositions`;
      }
    }
    void send({ path, body: JSON.stringify(body) });
  }

  const displayed = pinned?.item ?? latest;
  const diff = oldBasis && newBasis ? basisDiff(oldBasis, newBasis) : null;
  return <main>
    <header><h1>EFQA · основания решения</h1><p>DP-01 / API v2 · работа с готовыми наблюдениями, отдельное решение ОТК</p></header>
    <section><label>Bearer token (только память вкладки)<input type="password" value={token} disabled={locked} onChange={e => { setToken(e.target.value); setSelected(""); setLatest(null); setItems([]); dispatch({ type: "clear" }); }} /></label>
      <button disabled={!token || locked} onClick={() => void refresh()}>Загрузить изделия</button>
      <label>Изделие<select value={selected} disabled={locked} onChange={e => { setSelected(e.target.value); setLatest(null); setHistory(null); setTarget(""); setOldBasis(null); setNewBasis(null); dispatch({ type: "clear" }); }}><option value="">Выбрать</option>{items.map(x => <option key={x.item_id}>{x.item_id}</option>)}</select></label>
      <button disabled={!token} onClick={() => api.analytics(token).then(x => setCounts(x.counts)).catch(error)}>KPI сервера</button>
      {counts && <pre>{JSON.stringify(counts, null, 2)}</pre>}
    </section>
    <p role="status" className="message">{message}</p>
    {displayed && <>
      {stale && <p className="warning">Появилась ревизия {review.latestRevision}. Ниже остаётся закреплённая {pinned?.item.context_revision}; данные формы не заменены. Новая команда не отправлена: явно обновите рассмотрение.</p>}
      <section><h2>{displayed.item_id} · контекст {displayed.context_revision}</h2>
        <p>Цикл {displayed.control_cycle_id} · последнее решение: <strong>{displayed.disposition}</strong> · возможность нового выпуска: <strong>{displayed.release_allowed_now ? "по данным сервера разрешён" : "заблокирован"}</strong></p>
        <p>Допустимость нового выпуска не означает, что решение о выпуске уже принято или изделие отгружено.</p>
        <ul>{displayed.blockers.map(x => <li key={x}>{x}</li>)}</ul>
        <table><thead><tr><th>Точка</th><th>Цикл</th><th>Оценимость</th><th>Основания</th></tr></thead><tbody>{displayed.completeness.map(x => <tr key={x.checkpoint_id}><td>{x.checkpoint_id}</td><td>{x.control_cycle_id}</td><td>{x.assessment_status}</td><td>{x.record_ids.join(", ") || "нет"}</td></tr>)}</tbody></table>
        <h3>Несоответствия</h3>{displayed.nonconformities.map(x => <article key={x.id}><strong>{x.id}: {x.status} / {x.resolution_status}</strong><p>Причина {x.cause.status} / {x.cause.category}; review: {String(x.review_required)}</p><p>{x.cause.limitations.join("; ")}</p></article>)}
        <button onClick={() => api.timeline(selected, token).then(setHistory).catch(error)}>История с обоими временами</button>
        {history && <pre>{JSON.stringify(history, null, 2)}</pre>}
      </section>
      <section><h2>Отдельное рассмотрение</h2>
        <label>Цель<select value={target} disabled={locked || review.phase === "idle" && pinned !== null} onChange={e => setTarget(e.target.value)}><option value="">Решение по изделию / новый цикл</option>{(latest ?? displayed).nonconformities.map(x => <option key={x.id} value={x.id}>Карточка {x.id}</option>)}</select></label>
        <button disabled={locked} onClick={() => void pin()}>Начать / явно обновить рассмотрение</button>
        <button disabled={locked} onClick={() => dispatch({ type: "clear" })}>Завершить просмотр и сменить цель</button>
        {pinned && <><p>B = {pinned.basis.basis_id}; revision = {pinned.basis.context_revision}; цель {pinned.targetId ?? "изделие"}</p>
          <table><thead><tr><th>Record / признак</th><th>Компонент / область</th><th>Метод</th><th>Результат контроля</th></tr></thead><tbody>{pinned.basis.analysis_snapshot.observations.map(obs => <tr key={obs.observation_id}><td>{obs.record_id} / {obs.defect_index ?? "контроль без признака"}</td><td>{obs.component_id ?? "неизвестно"} / {obs.region_id ?? "неизвестно"}</td><td>{obs.analyzer_version ?? "неизвестно"}</td><td>{obs.result} → {obs.effective_result}</td></tr>)}</tbody></table>
          <details><summary>Полное сохранённое основание B</summary><pre>{JSON.stringify(pinned.basis, null, 2)}</pre></details>
          <label>Действие<select value={kind} disabled={locked} onChange={e => setKind(e.target.value)}>{["confirm", "reject", "request_check", "resolve", "reopen", "rework", "released", "rejected", "cycle"].map(x => <option key={x}>{x}</option>)}</select></label>
          <label>Причина<textarea value={reason} disabled={locked} onChange={e => setReason(e.target.value)} /></label>
          {(kind === "resolve" || kind === "cycle") && <label>Run ID доработки / планируемые run через запятую<input value={run} disabled={locked} onChange={e => setRun(e.target.value)} /></label>}
          {kind === "resolve" && <><label>Предыдущий run<input value={previousRun} disabled={locked} onChange={e => setPreviousRun(e.target.value)} /></label><label>Raw Record ID повторного контроля<input value={controlRecords} disabled={locked} onChange={e => setControlRecords(e.target.value)} /></label></>}
          {kind === "cycle" && <><label>ID решения rework<input value={reworkDecision} disabled={locked} onChange={e => setReworkDecision(e.target.value)} /></label>
            <label>Новые сессии: JSON checkpoint → session<textarea value={sessions} disabled={locked} onChange={e => setSessions(e.target.value)} /></label>
            <label>Операции: JSON checkpoint → run (incoming: null)<textarea value={bindings} disabled={locked} onChange={e => setBindings(e.target.value)} /></label></>}
          <button disabled={locked || stale || review.phase !== "idle" || !reason.trim()} onClick={submit}>Отправить отдельную команду</button>
          {review.phase === "unknown" && <button onClick={() => review.pending && void send(review.pending, true)}>Исход неизвестен: повторить точное тело и ключ</button>}
          {review.pending && <details><summary>Тело отправленной команды</summary><pre>{review.pending.body}</pre></details>}
        </>}
      </section>
      <section><h2>Тогда / сейчас</h2><p>Изменение оснований — не установленная физическая причина.</p>
        <label>Предыдущее basis_id из истории решений<input value={oldBasisId} onChange={e => setOldBasisId(e.target.value)} /></label>
        <button onClick={() => api.basis(oldBasisId, token).then(value => { if (value.item_id !== selected) throw new Error("Основание относится к другому изделию"); setOldBasis(value); }).catch(error)}>Открыть сохранённое основание</button>
        <button onClick={() => latest && api.basis(target ? latest.nonconformities.find(x => x.id === target)?.basis_id ?? latest.basis_id : latest.basis_id, token).then(setNewBasis).catch(error)}>Сравнить с текущим B (форма не меняется)</button>
        {diff && <pre>{JSON.stringify({ diff, then: oldBasis, now: newBasis }, null, 2)}</pre>}
        <h3>Решения</h3><pre>{JSON.stringify(displayed.decisions, null, 2)}</pre>
      </section>
    </>}
    <footer>Это клиент реального API, не симулятор ОТК. Фото не объявляются доступными. Полнота, права и переходы проверяются сервером.</footer>
  </main>;
}
