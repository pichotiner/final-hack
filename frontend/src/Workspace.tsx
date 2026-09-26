import { useState } from "react";
import type { Item } from "./api";

export type Page = "overview" | "items" | "analytics" | "integrations";
export const pages: Record<Page, { title: string; description: string; icon: string }> = {
  overview: { title: "Рабочее место ОТК", description: "Контроль, требующий вашего внимания", icon: "dashboard" },
  items: { title: "Изделия", description: "История производства и основания каждого решения", icon: "box" },
  analytics: { title: "Аналитика качества", description: "От показателя — к событиям и подтверждённым причинам", icon: "chart" },
  integrations: { title: "Обмен с ERP", description: "Производственные задания и доставка результатов", icon: "exchange" },
};

export function Icon({ name }: { name: string }) {
  const paths: Record<string, string> = {
    dashboard: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
    box: "m12 3 9 5v8l-9 5-9-5V8z M3 8l9 5 9-5 M12 13v8 M7.5 5.5l9 5",
    chart: "M4 3v17h17 M8 15v-4 M13 15V7 M18 15V4",
    exchange: "M3 7h17l-4-4 M21 17H4l4 4 M20 7l-4 4 M4 17l4-4",
    arrow: "M5 12h14 M13 6l6 6-6 6",
    shield: "M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7z M8 12l3 3 5-6",
    search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6",
    refresh: "M20 7a9 9 0 0 0-15-1L3 9 M3 3v6h6 M4 17a9 9 0 0 0 15 1l2-3 M21 21v-6h-6",
  };
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] ?? paths.box} /></svg>;
}

const labels: Record<string, string> = {
  pending: "Решение ожидается", released: "Выпуск разрешён", rework: "Назначена доработка", rejected: "Отклонено",
  pending_review: "На рассмотрении", confirmed: "Дефект подтверждён", needs_check: "Нужна проверка",
  not_applicable: "Устранение не требуется", open: "Не устранён", resolved: "Устранён",
  missing: "Нет контроля", not_assessable: "Недостаточно данных", assessable: "Контроль оценим", disputed: "Есть противоречия",
  unresolved: "Не установлено", hypothesis: "Гипотеза", established: "Установлена", unknown: "Неизвестно",
  incoming: "Входной дефект", equipment: "Оборудование", operator: "Действия оператора", other: "Другая причина",
  no_defect_signs: "Признаков нет", defect_signs: "Обнаружен признак", good: "Пригодное", poor: "Плохое качество",
  acknowledged: "Подтверждено ERP", pending_delivery: "В очереди", sending: "Отправляется", failed: "Ошибка доставки",
  completed: "Завершена", in_progress: "В работе", incomplete: "Не завершена", invalid: "Некорректные данные",
  item_received: "Изделие поступило", operation_started: "Начало операции", operation_finished: "Операция завершена",
  inspection_observed: "Контроль изделия", machine_state: "Состояние оборудования", operator_action: "Действие оператора", applied: "Учтено",
  "incoming-surface": "Входной контроль", "after-machining": "После обработки", "final-surface": "Финальный контроль",
  route_unknown: "Маршрут не определён", production_context_missing: "Нет производственного контекста",
  work_order_not_resolved: "Не определено производственное задание", assembly_not_resolved: "Не определён состав сборки",
  observation_cycle_unresolved: "Контроль не привязан к циклу", rework_operation_missing: "Нет операции доработки",
  context_review_required: "Новые данные требуют пересмотра", disposition_review_required: "Решение по изделию требует пересмотра",
  operation_incomplete: "Операция не завершена", scope_constraint: "Ограничение области контроля", nonconformity: "Открытое несоответствие", review_required: "Требуется пересмотр",
};
export const label = (value?: string | null): string => {
  if (!value) return "Неизвестно";
  if (labels[value]) return labels[value];
  const [key, ...parts] = value.split(":");
  return labels[key] ? `${labels[key]}: ${parts.map(part => labels[part] ?? part).join(" · ")}` : value;
};
export function Badge({ value }: { value: string }) {
  const tone = ["released", "resolved", "assessable", "acknowledged", "completed", "applied"].includes(value) ? "success"
    : ["confirmed", "open", "rejected", "failed", "disputed"].includes(value) ? "danger"
    : ["pending_review", "needs_check", "rework", "not_assessable", "missing"].includes(value) ? "attention" : "neutral";
  return <span className={`badge ${tone}`}><span className="status-dot" />{label(value)}</span>;
}
export const needsAttention = (item: Item) => item.review_required || item.blockers.length > 0 || item.nonconformities.some(c => c.review_required || c.status === "pending_review" || c.status === "needs_check");

export function ItemQueue({ items, connected, overview, busy, locked, onOpen, onRefresh }: {
  items: Item[]; connected: boolean; overview: boolean; busy: boolean; locked: boolean;
  onOpen: (id: string) => void; onRefresh: () => void;
}) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const filtered = items.filter(item => item.item_id.toLowerCase().includes(search.toLowerCase()) &&
    (filter === "all" || filter === "attention" && needsAttention(item) || filter === "review" && item.review_required || filter === "ready" && item.release_allowed_now));
  const sorted = [...filtered].sort((a, b) => Number(b.review_required) - Number(a.review_required) || Number(needsAttention(b)) - Number(needsAttention(a)) || a.item_id.localeCompare(b.item_id));
  return <>
    {overview && <div className="metric-grid" aria-label="Сводка по загруженным изделиям">
      {[
        { title: "Изделий в системе", value: items.length, detail: "Загруженный список", tone: "blue", filter: "all" },
        { title: "Требуют внимания", value: items.filter(needsAttention).length, detail: "Контроль и открытые вопросы", tone: "amber", filter: "attention" },
        { title: "На пересмотр", value: items.filter(i => i.review_required).length, detail: "Изменились основания решения", tone: "red", filter: "review" },
        { title: "Допускают выпуск", value: items.filter(i => i.release_allowed_now).length, detail: "Нужно отдельное решение ОТК", tone: "green", filter: "ready" },
      ].map(metric => <button key={metric.title} className={`metric ${metric.tone}`} onClick={() => setFilter(metric.filter)} aria-pressed={filter === metric.filter}>
        <span>{metric.title}</span><strong>{connected ? metric.value.toLocaleString("ru-RU") : "—"}</strong><small>{metric.detail}</small>
      </button>)}
    </div>}
    <section className="queue-panel">
      <div className="section-heading"><div><span className="eyebrow">{overview ? "ПРИОРИТЕТЫ КОНТРОЛЯ" : "РЕЕСТР ПРОИЗВОДСТВА"}</span><h2>{overview ? "Очередь решений" : "Все изделия"} <span className="count">{items.length}</span></h2></div>
        <button className="secondary" disabled={!connected || busy || locked} onClick={onRefresh}><Icon name="refresh" />{busy ? "Загрузка…" : "Обновить"}</button></div>
      <div className="queue-toolbar"><div className="filter-tabs" aria-label="Фильтр изделий">{[["all", "Все"], ["attention", "Требуют внимания"], ["review", "Пересмотр"], ["ready", "Допускают выпуск"]].map(([key, text]) => <button key={key} aria-pressed={filter === key} className={filter === key ? "active" : ""} onClick={() => setFilter(key)}>{text}</button>)}</div>
        <label className="search-field"><Icon name="search" /><input aria-label="Поиск изделия" placeholder="Найти изделие…" value={search} onChange={e => setSearch(e.target.value)} /></label></div>
      {!connected ? <div className="empty-state"><span className="empty-icon"><Icon name="shield" /></span><h3>Подключите рабочее место</h3><p>Введите токен доступа выше. Здесь появятся изделия,<br />состояние контроля и задачи для принятия решения.</p></div>
        : !sorted.length ? <div className="empty-state"><Icon name="search" /><h3>{items.length ? "Нет изделий по выбранным условиям" : "Изделия пока не поступили"}</h3><p>{items.length ? "Измените поиск или фильтр." : "Обновите список после поступления производственных событий."}</p></div>
        : <div className="table-scroll"><table className="queue-table"><thead><tr><th>Изделие</th><th>Последнее решение</th><th>Полнота контроля</th><th>Требует внимания</th><th><span className="sr-only">Действие</span></th></tr></thead><tbody>{sorted.map(item => {
          const covered = item.completeness.filter(c => c.assessment_status === "assessable").length;
          return <tr key={item.item_id}><td><button className="item-link" disabled={locked} onClick={() => onOpen(item.item_id)}><Icon name="box" />{item.item_id}</button><small className="cell-detail">Ревизия {item.context_revision}</small></td>
            <td><Badge value={item.disposition} /></td><td><span className="coverage-label">{covered} / {item.completeness.length} точек</span><progress value={covered} max={item.completeness.length || 1} aria-label={`Полнота контроля ${item.item_id}`} /></td>
            <td>{item.review_required ? <span className="attention-text">Изменились основания</span> : item.blockers.length ? <span>{item.blockers.length} препятствий для выпуска</span> : item.nonconformities.some(c => c.review_required || c.status === "pending_review" || c.status === "needs_check") ? "Рассмотрение несоответствия" : item.release_allowed_now ? "Доступно решение о выпуске" : "Нет активных задач"}</td>
            <td><button className="row-action" aria-label={`Открыть ${item.item_id}`} disabled={locked} onClick={() => onOpen(item.item_id)}><Icon name="arrow" /></button></td></tr>;
        })}</tbody></table></div>}
      <div className="table-footer"><span>Показано {sorted.length} из {items.length}</span><span>Приоритет: пересмотр и препятствия для выпуска</span></div>
    </section>
  </>;
}
