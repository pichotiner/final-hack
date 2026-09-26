import { useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError, request, verifyCommandReceipt, type Basis, type Item, type ReviewCommand, type DispositionCommand, type ResolutionCommand, type CycleCommand, type RecoveryCommand, type ScopeCommand } from "./api";
import { basisDiff, initialState, reviewReducer, type SavedCommand } from "./review-state";
import { BasisExplanation, SuggestedChecks } from "./BasisExplanation";
import { AnalyticsPanel } from "./AnalyticsPanel";
import { Badge, Icon, ItemQueue, label, pages, type Page } from "./Workspace";

const actions: Record<string, string> = { confirm: "Подтвердить дефект", reject: "Отклонить ложный признак", request_check: "Запросить проверку", resolve: "Зафиксировать устранение", reopen: "Открыть дефект повторно", rework: "Назначить доработку", released: "Разрешить выпуск", rejected: "Отклонить изделие", cycle: "Открыть цикл повторного контроля", bind_control: "Исправить привязку контроля", replace_control: "Заменить плохой контроль повторным", scope: "Разрешить ограничение области" };

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [receipt, setReceipt] = useState<Record<string, unknown> | null>(null);
  const [token, setToken] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [selected, setSelected] = useState("");
  const [latest, setLatest] = useState<Item | null>(null);
  const [history, setHistory] = useState<Awaited<ReturnType<typeof api.timeline>> | null>(null);
  const [message, setMessage] = useState("");
  const [review, dispatch] = useReducer(reviewReducer, initialState);
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState("confirm");
  const [target, setTarget] = useState("");
  const [run, setRun] = useState("");
  const [previousRun, setPreviousRun] = useState("");
  const [controlRecords, setControlRecords] = useState<string[]>([]);
  const [reworkDecision, setReworkDecision] = useState("");
  const [plannedRuns, setPlannedRuns] = useState<Record<string, string>>({});
  const [bindings, setBindings] = useState<Record<string, string | null>>({});
  const [record, setRecord] = useState("");
  const [replacement, setReplacement] = useState("");
  const [bindingCycle, setBindingCycle] = useState("");
  const [actorId, setActorId] = useState("");
  const [newToken, setNewToken] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [causeCategory, setCauseCategory] = useState("unknown");
  const [causeRecords, setCauseRecords] = useState<string[]>([]);
  const [causeRun, setCauseRun] = useState("");
  const [causeAction, setCauseAction] = useState("");
  const [scope, setScope] = useState<Awaited<ReturnType<typeof api.scope>> | null>(null);
  const [constraint, setConstraint] = useState("");
  const inFlight = useRef(false);
  const reviewRequest = useRef(0);
  const comparisonGeneration = useRef(0);
  const comparisonReads = useRef({ old: 0, new: 0 });
  const [oldBasis, setOldBasis] = useState<Basis | null>(null);
  const [oldBasisId, setOldBasisId] = useState("");
  const [newBasis, setNewBasis] = useState<Basis | null>(null);
  const [lineId, setLineId] = useState("line-1");
  const [orders, setOrders] = useState<Awaited<ReturnType<typeof api.orders>> | null>(null);
  const [line, setLine] = useState<Awaited<ReturnType<typeof api.line>> | null>(null);
  const [deliveryToken, setDeliveryToken] = useState("");
  const [deliveries, setDeliveries] = useState<Awaited<ReturnType<typeof api.deliveries>> | null>(null);
  const locked = review.phase === "sending" || review.phase === "unknown";
  const pinned = review.pinned;
  const stale = pinned !== null && review.latestRevision !== pinned.item.context_revision;

  const error = (value: unknown) => setMessage(value instanceof Error ? value.message : String(value));
  useEffect(() => {
    comparisonGeneration.current += 1;
    setOldBasis(null); setNewBasis(null); setOldBasisId("");
  }, [selected, token, target]);

  async function loadComparison(id: string, side: "old" | "new") {
    const generation = comparisonGeneration.current;
    const requestId = ++comparisonReads.current[side];
    try {
      const value = await api.basis(id, token);
      if (generation !== comparisonGeneration.current || requestId !== comparisonReads.current[side]) return;
      if (value.item_id !== selected || value.basis_id !== id) throw new Error("Получено основание другого изделия или с другим ID");
      if (side === "old") { setOldBasis(value); setOldBasisId(value.basis_id); }
      else setNewBasis(value);
    } catch (e) {
      if (generation === comparisonGeneration.current && requestId === comparisonReads.current[side]) error(e);
    }
  }
  async function refresh() {
    setBusy(true);
    try { const identity = await api.session(token); setActorId(identity.actor_id); setItems((await api.items(token)).items); setConnected(true); setMessage("Список изделий обновлён."); } catch (e) { error(e); } finally { setBusy(false); }
  }
  async function reauthenticate() {
    if (review.phase !== "unknown" || !review.pending?.actorId) return;
    setAuthBusy(true);
    try {
      const identity = await api.session(newToken);
      if (identity.actor_id !== review.pending.actorId) throw new Error("Нужна та же личность: " + review.pending.actorId);
      setToken(newToken); setNewToken(""); setActorId(identity.actor_id);
      setMessage("Доступ восстановлен. Исходное тело и ключ сохранены; повторите команду.");
    } catch (e) { error(e); } finally { setAuthBusy(false); }
  }
  useEffect(() => {
    if (!selected || !token) return;
    let active = true;
    async function poll() {
      try {
        const value = await api.item(selected, token);
        if (!active) return;
        setLatest(value); setItems(current => current.map(item => item.item_id === value.item_id ? value : item)); dispatch({ type: "poll", revision: value.context_revision });
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
    const requestId = ++reviewRequest.current;
    try {
      if (reason.trim() && pinned && !window.confirm("Перенести черновик причины на явно обновлённые основания?")) setReason("");
      const item = await api.item(selected, token);
      if (requestId !== reviewRequest.current) return;
      const card = target ? item.nonconformities.find(x => x.id === target) : null;
      if (target && !card) throw new Error("Карточка не принадлежит выбранному изделию");
      const basis = await api.basis(card ? card.basis_id : item.basis_id, token);
      if (requestId !== reviewRequest.current) return;
      if (basis.item_id !== item.item_id || basis.context_revision !== item.context_revision ||
          basis.control_cycle_id !== item.control_cycle_id || basis.target_id !== (target || null)) {
        throw new Error("Основание не соответствует полученному контексту. Повторите открытие рассмотрения.");
      }
      dispatch({ type: "pin", value: { item, basis, targetId: target || null } });
      setRun(""); setPreviousRun(""); setControlRecords([]); setRecord(""); setReplacement("");
      setBindingCycle(item.control_cycle_id); setPlannedRuns({}); setBindings({});
      setReworkDecision(item.decisions.filter(d => d.action === "rework").at(-1)?.decision_id ?? "");
      setCauseCategory("unknown"); setCauseRecords([]); setCauseRun(""); setCauseAction(""); setScope(null);
      setLatest(item); setNewBasis(basis);
      setMessage(`Закреплено основание ${basis.basis_id}, ревизия ${basis.context_revision}`);
    } catch (e) { if (requestId === reviewRequest.current) error(e); }
  }

  function clearReview() {
    reviewRequest.current += 1;
    dispatch({ type: "clear" });
  }

  async function transmit(command: SavedCommand) {
    try {
      const result = await request<Record<string, unknown>>(command.path, token, command.body);
      verifyCommandReceipt(command.path, command.body, result);
      dispatch({ type: "success" });
      setReceipt(result);
      setMessage(`Решение сохранено · ${result.decision_id ?? result.control_cycle_id}. Для следующего действия обновите рассмотрение.`);
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
    reviewRequest.current += 1;
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
    let body: ReviewCommand | DispositionCommand | ResolutionCommand | CycleCommand | RecoveryCommand | ScopeCommand;
    if (["confirm", "reject", "request_check"].includes(kind)) {
      if (!pinned.targetId) return setMessage("Выберите карточку и начните её рассмотрение");
      body = { ...common, decision: kind as ReviewCommand["decision"] }; path = `${cardPath}/decisions`;
      if (kind === "confirm" && causeCategory !== "unknown") {
        if (!causeRecords.length) return setMessage("Выберите записи, подтверждающие установленную причину");
        const operation = pinned.item.operations?.find(o => o.operation_run_id === causeRun);
        if (causeCategory === "operator" && (!operation?.operator_id || !causeAction)) return setMessage("Выберите операцию с известным оператором и его действие");
        body.cause = { status: "established", category: causeCategory as "incoming" | "equipment" | "operator" | "other", record_ids: causeRecords, limitations: [],
          ...(causeCategory === "operator" ? { operation_run_id: causeRun, operator_id: operation!.operator_id!, action_record_id: causeAction } : {}) };
      }
    } else if (kind === "resolve" || kind === "reopen") {
      if (!pinned.targetId) return setMessage("Выберите карточку");
      body = kind === "reopen" ? { ...common, action: "reopen" } : {
        ...common, action: "resolve", rework_operation_run_id: run,
        previous_operation_run_id: previousRun, control_record_ids: controlRecords,
      };
      if (kind === "resolve" && (!run || !previousRun || !controlRecords.length)) return setMessage("Выберите завершённую доработку и записи повторного контроля");
      path = `${cardPath}/resolutions`;
    } else {
      if (pinned.targetId !== null) return setMessage("Откройте рассмотрение изделия, не карточки");
      if (kind === "bind_control" || kind === "replace_control") {
        if (!record || (kind === "bind_control" ? !bindingCycle : !replacement)) return setMessage("Выберите контроль и его исправление");
        body = { ...common, action: kind, record_id: record, ...(kind === "bind_control" ? { control_cycle_id: bindingCycle } : { replacement_record_id: replacement }) };
        path = `${itemPath}/control-recoveries`;
      } else if (kind === "scope") {
        if (!scope || !constraint || scope.bases[pinned.item.item_id]?.basis_id !== pinned.basis.basis_id) return setMessage("Обновите ограничения области по закреплённому рассмотрению");
        body = { reason: common.reason, idempotency_key: common.idempotency_key, bases: scope.bases };
        path = `/api/v2/scope-constraints/${encodeURIComponent(constraint)}/resolutions`;
      } else if (kind === "cycle") {
        if (!reworkDecision || !Object.keys(plannedRuns).length) return setMessage("Выберите решение о доработке и повторяемые операции");
        const control_sessions = Object.fromEntries(pinned.item.completeness.map(cp => [cp.checkpoint_id, crypto.randomUUID()]));
        const checkpoint_operation_run_ids: Record<string, string | null> = {};
        for (const cp of pinned.item.completeness) {
          const bound = cp.operation_run_id === null ? null : bindings[cp.checkpoint_id];
          if (bound === undefined || bound !== null && !Object.values(plannedRuns).includes(bound)) return setMessage("Выберите планируемую операцию для каждой точки контроля");
          checkpoint_operation_run_ids[cp.checkpoint_id] = bound;
        }
        body = { ...common, rework_disposition_id: reworkDecision, planned_operation_run_ids: Object.values(plannedRuns), control_sessions, checkpoint_operation_run_ids };
        path = `${itemPath}/control-cycles`;
      } else {
        body = { ...common, disposition: kind as DispositionCommand["disposition"] };
        path = `${itemPath}/dispositions`;
      }
    }
    if (!actorId) return setMessage("Загрузите изделия, чтобы проверить вашу личность");
    void send({ path, body: JSON.stringify(body), actorId });
  }

  const displayed = pinned?.item ?? latest;
  const diff = oldBasis && newBasis && oldBasis.item_id === newBasis.item_id && oldBasis.target_id === newBasis.target_id
    && oldBasis.context_revision <= newBasis.context_revision ? basisDiff(oldBasis, newBasis) : null;
  const records = [...new Map((pinned?.item.observations ?? []).filter(o => o.record_id).map(o => [o.record_id!, o])).values()];
  const recordLabel = (o: typeof records[number]) => `${o.checkpoint_id} · ${o.occurred_at ?? "время неизвестно"} · ${o.effective_result} · ${o.record_id}`;
  function openItem(id: string) {
    if (locked || busy) return;
    setSelected(id); setPage("items"); setLatest(null); setHistory(null); setTarget(""); setKind("rework"); setOldBasis(null); setNewBasis(null); setReason(""); setReceipt(null); setMessage(""); clearReview();
    window.scrollTo({ top: 0 });
  }
  function navigate(next: Page) {
    setPage(next);
    window.scrollTo({ top: 0 });
  }
  const availableActions = Object.entries(actions).filter(([key]) => target
    ? ["confirm", "reject", "request_check", "resolve", "reopen"].includes(key)
    : ["rework", "released", "rejected", "cycle", "bind_control", "replace_control", "scope"].includes(key));
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Icon name="shield" /></span><div>EFQA<span>QUALITY INTELLIGENCE</span></div></div>
      <div className="nav-caption">ПРОИЗВОДСТВЕННЫЙ КОНТУР</div>
      <nav aria-label="Основная навигация">{(Object.entries(pages) as [Page, typeof pages[Page]][]).map(([key, value]) => <button key={key} className={page === key ? "nav-item active" : "nav-item"} aria-current={page === key ? "page" : undefined} onClick={() => navigate(key)}><Icon name={value.icon} />{value.title}{key === "overview" && connected && <span className="nav-count">{items.filter(i => i.review_required).length}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><Icon name="shield" /><div>Доказательная модель<span>Наблюдение → основание → решение</span></div></div>
      <div className="demo-label">Демонстратор · синтетические данные</div>
    </aside>
    <div className="workspace">
    <header className="topbar"><span>Контроль качества <span className="breadcrumb-slash">/</span> <strong>{pages[page].title}</strong></span><div className="session-chip"><span className={`status-dot ${connected ? "online" : ""}`} />{connected ? actorId : "Нет подключения"}<span className="avatar">{connected ? actorId.slice(0, 2).toUpperCase() : "ОТ"}</span></div></header>
    <main>
    <div className="page-heading"><div><span className="eyebrow">EVIDENCE-FIRST QUALITY</span><h1>{page === "items" && selected ? "Паспорт изделия" : pages[page].title}</h1><p>{pages[page].description}</p></div><span className="environment-tag">ДЕМО / API v2</span></div>
    <details className="connection-panel" open={!connected || review.phase === "unknown"}><summary><Icon name="shield" />{connected ? `Сеанс: ${actorId}` : "Подключение к системе"}<span>Токен хранится только в памяти вкладки</span></summary>
      <div className="connection-fields"><label>Bearer token (только память вкладки)<input type="password" autoComplete="off" placeholder="Введите токен доступа" value={token} disabled={locked || busy} onChange={e => { setToken(e.target.value); setConnected(false); setActorId(""); setSelected(""); setLatest(null); setHistory(null); setReceipt(null); setItems([]); setOrders(null); setLine(null); setDeliveryToken(""); setDeliveries(null); clearReview(); }} /></label>
      <button className="primary" disabled={!token || locked || busy} onClick={() => void refresh()}>{busy ? "Подключение…" : "Загрузить изделия"}</button></div>
      {review.phase === "unknown" && <fieldset><legend>Восстановление доступа для {review.pending?.actorId}</legend><label>Новый токен той же личности<input type="password" value={newToken} disabled={authBusy} onChange={e => setNewToken(e.target.value)} /></label><button disabled={!newToken || authBusy} onClick={() => void reauthenticate()}>Восстановить доступ, сохранив команду</button></fieldset>}
    </details>
    <p role="status" className={`message ${message ? "" : "is-empty"}`}>{message}</p>
    {locked && <div className="warning">{review.phase === "sending" ? "Сохраняем решение. Дождитесь ответа сервера." : "Ответ на команду не получен. Откройте изделие и повторите сохранённую команду — её тело и ключ сохранены."}<button onClick={() => setPage("items")}>К решению</button></div>}
    {(page === "overview" || page === "items" && !selected) && <ItemQueue items={items} connected={connected} overview={page === "overview"} busy={busy} locked={locked || busy} onOpen={openItem} onRefresh={() => void refresh()} />}
    {page === "integrations" && <section><div className="section-heading"><div><span className="eyebrow">ВХОДЯЩИЙ ПОТОК</span><h2>Производственные задания</h2></div><button disabled={!token} onClick={() => api.orders(token).then(setOrders).catch(error)}>Задания ERP</button></div>
      {orders && <div><h2>Полученные задания ERP — план, не факт выполнения</h2><table aria-label="Задания ERP"><thead><tr><th>Задание / revision</th><th>Маршрут</th><th>Экземпляры</th></tr></thead><tbody>{orders.items.map(order => <tr key={order.id}><td>{order.external_order_id} / {order.revision}</td><td>{order.route_id}@{order.route_version}</td><td>{order.item_ids.length}</td></tr>)}</tbody></table><details><summary>Профиль и полный вход ERP</summary><pre>{JSON.stringify(orders, null, 2)}</pre></details></div>}
      {!orders && <p className="muted">Загрузите задания, чтобы увидеть маршрут и состав партии из внешней системы.</p>}
    </section>}
    <div hidden={page !== "analytics"}><AnalyticsPanel key={actorId} token={token} items={items} /></div>
    {page === "overview" && <section className="line-panel"><div className="section-heading"><div><span className="eyebrow">ПРОИЗВОДСТВО</span><h2>Состояние линии</h2></div><Icon name="exchange" /></div>
      <div className="inline-fields">
      <label>Номер линии<input value={lineId} onChange={e => { setLineId(e.target.value); setLine(null); }} /></label>
      <button disabled={!token || !lineId.trim()} onClick={() => api.line(lineId.trim(), token).then(setLine).catch(error)}>Состояние линии</button>
      </div>
      {line && <div><p>Снимок линии {line.line_id} на {line.computed_at}. Последний приём: {line.source_last_received_at ?? "нет данных"}.</p>
        <p>Изделий с неразрешёнными связями: {line.unresolved_items}. Активные операции: {line.active_operation_run_ids.join(", ") || "нет по данным сервера"}.</p></div>}
      {!line && <p className="muted">Запросите актуальный снимок линии: активные операции и неразрешённые связи.</p>}
    </section>}
    {page === "items" && selected && <div className="item-toolbar"><button disabled={locked} onClick={() => { setSelected(""); clearReview(); }}>← Все изделия</button><label>Изделие<select value={selected} disabled={locked} onChange={e => openItem(e.target.value)}>{items.map(x => <option key={x.item_id}>{x.item_id}</option>)}</select></label><span className="muted">Проверка обновлений каждые 5 секунд</span></div>}
    {page === "items" && selected && !displayed && <section className="empty-state"><h2>Загружаем паспорт изделия…</h2><p>Получаем состояние контроля и историю решений.</p></section>}
    {page === "items" && displayed && selected && <>
      {stale && <p className="warning">Появилась ревизия {review.latestRevision}. Ниже остаётся закреплённая {pinned?.item.context_revision}; данные формы не заменены. Новая команда не отправлена: явно обновите рассмотрение.</p>}
      <section className="item-summary"><div className="section-heading"><div><span className="eyebrow">ПАСПОРТ ИЗДЕЛИЯ · РЕВИЗИЯ {displayed.context_revision}</span><h2 className="item-title"><Icon name="box" />{displayed.item_id}</h2></div><Badge value={displayed.disposition} /></div>
        <div className={`release-banner ${displayed.release_allowed_now ? "allowed" : "blocked"}`}><Icon name="shield" /><div><strong>{displayed.release_allowed_now ? "Доступно решение о выпуске" : "Выпуск заблокирован"}</strong><span>{displayed.release_allowed_now ? "Обязательные условия выполнены. Выпуск оформляется отдельным решением ОТК." : "Для нового выпуска необходимо устранить препятствия ниже."}</span></div>{displayed.review_required && <span className="badge attention">Требуется пересмотр</span>}</div>
        {displayed.blockers.length > 0 && <ul className="blocker-list">{displayed.blockers.map(x => <li key={x}>{label(x)}</li>)}</ul>}
        <h3>Карта обязательного контроля</h3><div className="checkpoint-grid">{displayed.completeness.map((x, i) => <div className="checkpoint" key={x.checkpoint_id}><span className="step-number">{String(i + 1).padStart(2, "0")}</span><strong>{label(x.checkpoint_id)}</strong><Badge value={x.assessment_status} /><details><summary>Основания · {x.record_ids.length}</summary><p>Точка: {x.checkpoint_id} · цикл: {x.control_cycle_id}</p><p>{x.record_ids.join(", ") || "Нет записей контроля"}</p></details></div>)}</div>
        {!displayed.completeness.length && <p className="muted">Контрольные точки не получены.</p>}
        <h3>Несоответствия <span className="count">{displayed.nonconformities.length}</span></h3>{displayed.nonconformities.map((x, index) => <article className="defect-card" key={x.id}><div className="section-heading"><strong>Несоответствие {String(index + 1).padStart(2, "0")}</strong><Badge value={x.status} /><Badge value={x.resolution_status} /></div><p>Причина: {label(x.cause.status)} · {label(x.cause.category)}{x.review_required ? " · Требуется пересмотр" : ""}</p><p>{x.cause.limitations.join("; ")}</p><button disabled={locked || !!pinned} onClick={() => { clearReview(); setTarget(x.id); setKind("confirm"); document.getElementById("review-panel")?.scrollIntoView({ behavior: "smooth" }); }}>Рассмотреть дефект <Icon name="arrow" /></button><details><summary>Идентификатор и записи</summary><p>{x.id}</p><p>{x.record_ids.join(", ")}</p></details></article>)}
        {!displayed.nonconformities.length && <p className="muted">Карточек несоответствий нет. Возможность выпуска определяется полнотой контроля.</p>}
        <details className="history-panel"><summary>Производственная история</summary>
        <button onClick={() => api.timeline(selected, token).then(setHistory).catch(error)}>История с обоими временами</button>
        {history && <><table aria-label="История событий"><thead><tr><th>Событие / источник</th><th>Произошло</th><th>Принято</th><th>Обработка</th></tr></thead><tbody>{history.items.map(entry => <tr key={entry.record_id}><td>{label(entry.event_type)}<br />{entry.source_id}<br />{entry.record_id}</td><td>{entry.occurred_at}</td><td>{entry.received_at}</td><td><Badge value={entry.projection_status} /></td></tr>)}</tbody></table><details><summary>Подробности истории</summary><pre>{JSON.stringify(history, null, 2)}</pre></details></>}
        </details>
      </section>
      <section id="review-panel" className="review-panel"><span className="eyebrow">РАБОЧАЯ ОБЛАСТЬ КОНТРОЛЁРА</span><h2>Рассмотрение и решение</h2><p className="muted">Откройте основания, изучите наблюдения и зафиксируйте своё решение.</p>
        <label>Цель<select value={target} disabled={locked || review.phase === "idle" && pinned !== null} onChange={e => { clearReview(); setTarget(e.target.value); setKind(e.target.value ? "confirm" : "rework"); }}><option value="">Решение по изделию / новый цикл</option>{(latest ?? displayed).nonconformities.map((x, i) => <option key={x.id} value={x.id}>Несоответствие {i + 1} · {label(x.status)}</option>)}</select></label>
        <button disabled={locked} onClick={() => void pin()}>Начать / явно обновить рассмотрение</button>
        <button disabled={locked} onClick={clearReview}>Завершить просмотр и сменить цель</button>
        {pinned && <><div className="pinned-note"><Icon name="shield" /><span>Основания закреплены · ревизия {pinned.basis.context_revision}. Новые события не изменят вашу форму.</span></div><details><summary>Версия и идентификаторы рассмотрения</summary><p>B = {pinned.basis.basis_id}; revision = {pinned.basis.context_revision}; цель {pinned.targetId ?? "изделие"}</p></details>
          <table><thead><tr><th>Record / признак</th><th>Компонент / область</th><th>Метод</th><th>Результат контроля</th></tr></thead><tbody>{pinned.basis.analysis_snapshot.observations.map(obs => <tr key={obs.observation_id}><td>{obs.record_id} / {obs.defect_index ?? "контроль без признака"}</td><td>{obs.component_id ?? "неизвестно"} / {obs.region_id ?? "неизвестно"}</td><td>{obs.analyzer_version ?? "неизвестно"}</td><td>{obs.result} → {obs.effective_result}</td></tr>)}</tbody></table>
          <details><summary>Полное сохранённое основание B</summary><pre>{JSON.stringify(pinned.basis, null, 2)}</pre></details>
          <SuggestedChecks basis={pinned.basis} />
          {(pinned.basis.analysis_snapshot.circumstances ?? []).filter(c => !pinned.targetId || c.nonconformity_id === pinned.targetId).map(c => <article key={c.nonconformity_id}><h3>Разбор обстоятельств дефекта</h3><p>Сопоставление: компонент {c.comparison?.component_id ?? "неизвестен"}, область {c.comparison?.region_id ?? "неизвестна"}, метод {c.comparison?.method_version ?? "неизвестен"}.</p><p>До: {c.before_record_id ?? "нет сопоставимого контроля"}; после: {c.after_record_id ?? "нет пригодного осмотра"}.</p><p>Операции интервала: {c.operation_run_ids.join(", ") || "не установлены"}</p><ul>{c.alternatives.map((a, i) => <li key={i}>{a.explanation} Основания: {a.record_ids.join(", ") || "нет"}</li>)}</ul><ul>{c.limitations.map(l => <li key={l}>{l}</li>)}</ul>{c.related_events.map(e => <p key={e.record_id}>{e.occurred_at} · {e.event_type} · {e.equipment_id ?? e.operator_id} · запись {e.record_id}</p>)}</article>)}
          <div className="decision-form"><h3>Зафиксировать решение</h3><label>Действие<select value={kind} disabled={locked} onChange={e => setKind(e.target.value)}>{availableActions.map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
          <label>Причина<textarea value={reason} disabled={locked} onChange={e => setReason(e.target.value)} /></label>
          {kind === "confirm" && <><label>Установленная причина<select value={causeCategory} disabled={locked} onChange={e => setCauseCategory(e.target.value)}><option value="unknown">Не установлена</option><option value="incoming">Входной дефект</option><option value="equipment">Оборудование</option><option value="operator">Подтверждённая ошибка оператора</option><option value="other">Другая</option></select></label>
            {causeCategory !== "unknown" && <label>Основания установленной причины<select multiple value={causeRecords} disabled={locked} onChange={e => setCauseRecords(Array.from(e.target.selectedOptions, o => o.value))}>{pinned.basis.inputs.map(e => <option key={e.record_id} value={e.record_id}>{e.occurred_at} · {pinned.item.timeline?.find(t => t.record_id === e.record_id)?.event_type ?? e.event_id} · {e.record_id}</option>)}</select></label>}
            {causeCategory === "operator" && <><label>Операция и оператор<select value={causeRun} disabled={locked} onChange={e => { setCauseRun(e.target.value); setCauseAction(""); }}><option value="">Выбрать</option>{pinned.item.operations?.filter(o => o.operator_id).map(o => <option key={o.operation_run_id} value={o.operation_run_id}>{o.operation_type_id} · {o.operator_id} · {o.operation_run_id}</option>)}</select></label><label>Подтверждающее действие<select value={causeAction} disabled={locked} onChange={e => { setCauseAction(e.target.value); setCauseRecords([...new Set([...causeRecords, e.target.value])]); }}><option value="">Выбрать</option>{pinned.item.timeline?.filter(e => e.event_type === "operator_action" && e.operation_run_id === causeRun).map(e => <option key={e.record_id} value={e.record_id}>{e.occurred_at} · {e.operator_id} · {e.record_id}</option>)}</select></label></>}
          </>}
          {kind === "resolve" && <><label>Завершённая доработка<select value={run} disabled={locked} onChange={e => { setRun(e.target.value); setPreviousRun(pinned.item.operations?.find(o => o.operation_run_id === e.target.value)?.previous_operation_run_id ?? ""); setControlRecords([]); }}><option value="">Выбрать</option>{pinned.item.operations?.filter(o => o.previous_operation_run_id && o.status === "completed").map(o => <option key={o.operation_run_id} value={o.operation_run_id}>{o.operation_type_id} · {o.station_id} · {o.operation_run_id}</option>)}</select></label><p>Предыдущая операция: {previousRun || "выберите доработку"}</p><label>Записи повторного контроля<select multiple value={controlRecords} disabled={locked} onChange={e => setControlRecords(Array.from(e.target.selectedOptions, o => o.value))}>{records.filter(o => o.cycle_id === pinned.item.control_cycle_id && o.operation_run_id === run && o.effective_result === "no_defect_signs" && o.quality === "good").map(o => <option key={o.record_id} value={o.record_id!}>{recordLabel(o)}</option>)}</select></label></>}
          {kind === "cycle" && <><label>Решение о доработке<select value={reworkDecision} disabled={locked} onChange={e => setReworkDecision(e.target.value)}><option value="">Выбрать</option>{pinned.item.decisions.filter(d => d.action === "rework").map(d => <option key={d.decision_id} value={d.decision_id}>{d.reason} · {d.actor_id} · {d.decision_id}</option>)}</select></label>
            <label>Повторяемые операции<select multiple value={Object.keys(plannedRuns)} disabled={locked} onChange={e => { const next = Object.fromEntries(Array.from(e.target.selectedOptions, o => [o.value, plannedRuns[o.value] ?? crypto.randomUUID()])); setPlannedRuns(next); setBindings(Object.fromEntries(pinned.item.completeness.map(cp => [cp.checkpoint_id, cp.operation_run_id === null ? null : Object.values(next)[0] ?? null]))); }}>{pinned.item.operations?.map(o => <option key={o.operation_run_id} value={o.operation_run_id}>{o.operation_type_id} · {o.station_id} · {o.operation_run_id}</option>)}</select></label>
            <p>Новые идентификаторы операций и сессий создаются автоматически. Передайте план источнику производственных событий.</p>
            {pinned.item.completeness.filter(cp => cp.operation_run_id !== null).map(cp => <label key={cp.checkpoint_id}>Повторный контроль {cp.checkpoint_id}<select value={bindings[cp.checkpoint_id] ?? ""} disabled={locked} onChange={e => setBindings({ ...bindings, [cp.checkpoint_id]: e.target.value })}><option value="">Выбрать операцию</option>{Object.entries(plannedRuns).map(([old, value]) => <option key={value} value={value}>Повтор {old}</option>)}</select></label>)}
            <ul>{Object.entries(plannedRuns).map(([old, value]) => <li key={value}>Повтор {old} → {value}</li>)}</ul>
          </>}
          {(kind === "bind_control" || kind === "replace_control") && <><label>Исправляемый контроль<select value={record} disabled={locked} onChange={e => setRecord(e.target.value)}><option value="">Выбрать</option>{records.filter(o => kind === "bind_control" ? o.cycle_id === null : o.effective_result === "not_assessable").map(o => <option key={o.record_id} value={o.record_id!}>{recordLabel(o)}</option>)}</select></label>
            {kind === "bind_control" ? <label>Зарегистрированный цикл<select value={bindingCycle} disabled={locked} onChange={e => setBindingCycle(e.target.value)}>{Object.keys(pinned.item.cycles ?? {}).map(id => <option key={id}>{id}</option>)}</select></label> : <label>Пригодный повтор<select value={replacement} disabled={locked} onChange={e => setReplacement(e.target.value)}><option value="">Выбрать</option>{records.filter(o => o.record_id !== record && o.quality === "good" && o.effective_result !== "not_assessable").map(o => <option key={o.record_id} value={o.record_id!}>{recordLabel(o)}</option>)}</select></label>}</>}
          {kind === "scope" && <><button disabled={locked} onClick={() => api.scope(token).then(setScope).catch(error)}>Обновить ограничения области</button><label>Ограничение области<select value={constraint} disabled={locked} onChange={e => setConstraint(e.target.value)}><option value="">Выбрать</option>{scope?.constraints.map(c => <option key={c.constraint_id} value={c.constraint_id}>{c.reason} · {c.constraint_id}</option>)}</select></label><p>Решение проверяет основания всех изделий области: {Object.keys(scope?.bases ?? {}).join(", ") || "загрузите ограничения"}. Исходный неопределённый сигнал сохраняется.</p></>}
          <button className="primary" disabled={locked || stale || review.phase !== "idle" || !reason.trim()} onClick={submit}>Отправить отдельную команду</button>
          {review.phase === "unknown" && <button disabled={authBusy} onClick={() => review.pending && void send(review.pending, true)}>Исход неизвестен: повторить точное тело и ключ</button>}
          {review.pending && <details><summary>Тело отправленной команды</summary><pre>{review.pending.body}</pre></details>}
          {receipt && <details><summary>Квитанция сохранённого решения</summary><pre>{JSON.stringify(receipt, null, 2)}</pre></details>}
          </div>
        </>}
      </section>
      <section className="comparison-panel"><span className="eyebrow">ПРОСЛЕЖИВАЕМОСТЬ РЕШЕНИЙ</span><h2>Тогда / сейчас</h2><p>Что было известно при решении и что изменилось после него.</p>
        <label>Предыдущее основание из истории решений<select value={oldBasisId} onChange={e => setOldBasisId(e.target.value)}><option value="">Выбрать решение</option>{displayed.decisions.map(d => <option key={d.decision_id} value={d.basis_id}>{actions[d.action] ?? d.action} · {d.reason} · {d.basis_id}</option>)}</select></label>
        <button onClick={() => void loadComparison(oldBasisId, "old")}>Открыть сохранённое основание</button>
        <button onClick={() => {
          const id = target ? latest?.nonconformities.find(x => x.id === target)?.basis_id : latest?.basis_id;
          if (id) void loadComparison(id, "new"); else setMessage("Нет текущего B выбранной цели");
        }}>Сравнить с текущим B (форма не меняется)</button>
        {oldBasis && newBasis && <BasisExplanation before={oldBasis} after={newBasis} />}
        {diff && <details><summary>Подробные различия исходников</summary><pre>{JSON.stringify(diff, null, 2)}</pre></details>}
        <h3>Решения</h3><table aria-label="История решений"><thead><tr><th>Решение</th><th>Автор / причина</th><th>Основание тогда</th></tr></thead><tbody>{displayed.decisions.map(decision => <tr key={decision.decision_id}><td>{decision.action}<br />{decision.decision_id}</td><td>{decision.actor_id}<br />{decision.reason}</td><td><button onClick={() => void loadComparison(decision.basis_id, "old")}>{decision.basis_id}</button></td></tr>)}</tbody></table>
        {oldBasis && <details><summary>Сохранённое основание решения {oldBasis.basis_id}</summary><pre>{JSON.stringify(oldBasis, null, 2)}</pre></details>}
        <details><summary>Полные квитанции решений</summary><pre>{JSON.stringify(displayed.decisions, null, 2)}</pre></details>
      </section>
    </>}
    {page === "integrations" && <section><span className="eyebrow">ИСХОДЯЩИЙ ПОТОК</span><h2>Доставка результата в ERP</h2><p>ACK подтверждает сохранение результата получателем, а не физическую отгрузку.</p>
      <button disabled={!token} onClick={() => api.deliveries(token).then(setDeliveries).catch(error)}>Проверить доставки ERP</button><p className="muted">Для просмотра нужна роль integration/admin. Можно использовать отдельный токен ниже.</p>
      <label>Токен интеграции (только память вкладки)<input type="password" value={deliveryToken} onChange={e => { setDeliveryToken(e.target.value); setDeliveries(null); }} /></label>
      <button disabled={!deliveryToken} onClick={() => api.deliveries(deliveryToken).then(setDeliveries).catch(error)}>Обновить доставки и ACK</button>
      {deliveries && <><table aria-label="Доставки ERP"><thead><tr><th>Сообщение / версия</th><th>Статус</th><th>Попытки / ошибка</th></tr></thead><tbody>{deliveries.map(entry => <tr key={entry.message_id}><td>{entry.message_id} / {entry.result_version}</td><td><Badge value={entry.status} /></td><td>{entry.attempts} / {entry.last_error ?? "нет"}</td></tr>)}</tbody></table><details><summary>Сохранённые тела, digest и квитанции ACK</summary><pre>{JSON.stringify(deliveries, null, 2)}</pre></details></>}
    </section>}
    <footer><span>EFQA <span className="footer-dot">·</span> Контроль с проверяемыми основаниями</span><span>Структурированные наблюдения · фото не предоставлены</span></footer>
  </main></div></div>;
}
