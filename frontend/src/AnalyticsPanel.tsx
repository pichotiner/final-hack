import { useState } from "react";
import { api, type Analytics, type Item } from "./api";

const labels: Record<string, string> = {
  inspected_items: "Изделий проверено за всю историю", current_cycle_inspected_items: "Изделий с пригодным контролем активного цикла",
  confirmed_defects: "Подтверждённых дефектов", confirmed_nonconforming_items: "Изделий с подтверждёнными дефектами",
  open_confirmed_defects: "Открытых дефектов", resolved_confirmed_defects: "Устранённых дефектов",
  unresolved_cause_defects: "Причина не установлена", established_cause_defects: "Причина установлена",
  confirmed_operator_errors: "Операций с установленной ошибкой оператора", rework_operations: "Завершённых доработок",
  repeat_controls: "Повторных осмотров", blocked_items: "Изделий с запретом выпуска",
};
const known = (value?: string | null) => value ?? "неизвестно";
function Evidence({ row }: { row: { record_ids: string[]; decision_ids?: string[]; operation_run_ids?: string[] } }) {
  return <details><summary>Основания расчёта</summary><p>Записи: {row.record_ids.join(", ") || "нет"}</p>
    {row.decision_ids && <p>Решения: {row.decision_ids.join(", ") || "нет"}</p>}
    {row.operation_run_ids && <p>Операции: {row.operation_run_ids.join(", ")}</p>}</details>;
}
export function AnalyticsPanel({ token, items }: { token: string; items: Item[] }) {
  const [data, setData] = useState<Analytics | null>(null);
  const [cohort, setCohort] = useState<string[]>([]);
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [error, setError] = useState("");
  async function load() {
    try {
      const params = new URLSearchParams();
      cohort.forEach(id => params.append("item_id", id));
      if (since) params.set("since", new Date(since).toISOString());
      if (until) params.set("until", new Date(until).toISOString());
      setData(await api.analytics(token, params.toString())); setError("");
    } catch (e) { setError(String(e)); }
  }
  return <section><h2>Аналитика качества</h2>
    <label>Когорта (без выбора — все изделия)<select multiple value={cohort} onChange={e => setCohort(Array.from(e.target.selectedOptions, x => x.value))}>{items.map(i => <option key={i.item_id}>{i.item_id}</option>)}</select></label>
    <label>Период с (местное время)<input type="datetime-local" value={since} onChange={e => setSince(e.target.value)} /></label>
    <label>Период до, не включая<input type="datetime-local" value={until} onChange={e => setUntil(e.target.value)} /></label>
    <button disabled={!token} onClick={() => void load()}>KPI сервера</button>{error && <p role="alert">{error}</p>}
    {data && <><p>Когорта: {data.item_ids.join(", ") || "пустая"}. Рассчитано {data.computed_at}.</p>
      <p>Период: {data.period?.since ?? "начало истории"} — {data.period?.until ?? "сейчас"}; проверено изделий за период: {data.period_inspected_items}.</p>
      <ul>{data.definitions?.map(d => <li key={d}>{d}</li>)}</ul>
      <dl>{Object.entries(data.counts).map(([key, value]) => <div key={key}><dt>{labels[key] ?? key}</dt><dd>{value}</dd></div>)}</dl>
      <h3>Дефекты по типам, линиям и участкам обнаружения</h3><table><thead><tr><th>Тип / место</th><th>Причина</th><th>Дефектов</th><th>Основания</th></tr></thead><tbody>{data.defects?.map((row, i) => <tr key={i}><td>{known(row.defect_type)} / {known(row.line_id)} / {known(row.station_id)}</td><td>{row.cause_status} / {row.cause_category}</td><td>{row.confirmed_defects}</td><td><Evidence row={row} /></td></tr>)}</tbody></table>
      <h3>Длительности по изделию, участку, оператору и смене</h3><table><thead><tr><th>Изделие / участок</th><th>Оператор / смена</th><th>Интервал / источник</th><th>Секунды / измерения</th></tr></thead><tbody>{data.durations?.map((row, i) => <tr key={i}><td>{row.item_id} / {known(row.station_id)}</td><td>{known(row.operator_id)} / {known(row.shift_id)}</td><td>{row.kind} / {row.origin}</td><td>{row.measured_operations ? row.total_seconds : "нет измерений"}; измерено {row.measured_operations}, недоступно {row.unavailable_operations}<br />Завершено {row.completed_operations}, незавершено {row.incomplete_operations}, некорректно {row.invalid_operations}<Evidence row={row} /></td></tr>)}</tbody></table>
      <h3>Подтверждённые ошибки на сопоставимых работах</h3><table><thead><tr><th>Тип изделия / маршрут / операция</th><th>Оператор / смена</th><th>Числитель / знаменатель</th><th>Доля</th></tr></thead><tbody>{data.comparable_work?.map((row, i) => <tr key={i}><td>{known(row.item_type_id)} / {known(row.route_version)} / {known(row.operation_type_id)}</td><td>{known(row.operator_id)} / {known(row.shift_id)}</td><td>{row.confirmed_errors} / {row.completed_operations}<Evidence row={row} /></td><td>{row.error_rate === null ? "недостаточно данных" : `${(row.error_rate * 100).toFixed(1)}%`}</td></tr>)}</tbody></table>
    </>}
  </section>;
}
