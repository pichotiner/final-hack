import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiFailure, type Item, type Nonconformity } from "./api";

type View = "line" | "items" | "review" | "analytics" | "exchange";
const labels: Record<View, string> = {
  line: "Линия",
  items: "Изделия",
  review: "Рассмотрение",
  analytics: "Аналитика",
  exchange: "Обмен",
};
const status: Record<string, string> = {
  pending: "Ожидает решения",
  released: "Выпущено",
  rework: "Доработка",
  rejected: "Отклонено",
  pending_review: "На рассмотрении",
  confirmed: "Подтверждено",
  needs_check: "Нужна проверка",
  good: "Хорошее",
  poor: "Низкое",
  unknown: "Неизвестно",
  defect_signs: "Есть признаки",
  no_defect_signs: "Признаков нет",
  not_assessable: "Не оценено",
  acknowledged: "Подтверждено",
  retrying: "Повтор",
  failed: "Ошибка",
};
const human = (value: string | null | undefined) =>
  value == null || value === "" ? "Неизвестно" : status[value] || value;
const when = (value: string | null | undefined) =>
  value
    ? new Date(value).toLocaleString("ru-RU", {
        dateStyle: "short",
        timeStyle: "medium",
        timeZone: "UTC",
      }) + " UTC"
    : "Неизвестно";

function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const failure =
    error instanceof ApiFailure
      ? error
      : new ApiFailure(0, "unknown", "Не удалось получить данные.");
  return (
    <div className="error" role="alert">
      <strong>{failure.code}</strong>
      <span>{failure.message}</span>
      {failure.status === 401 || failure.status === 403 ? (
        <small>Проверьте права и токен доступа.</small>
      ) : null}
    </div>
  );
}
function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}
function Chip({ value }: { value: string | null | undefined }) {
  return (
    <span className={`chip chip-${value || "unknown"}`}>{human(value)}</span>
  );
}

function Components({ items }: { items: Item["components"] }) {
  if (!items.length) return <p className="muted">Состав не передан.</p>;
  return (
    <ul className="components">
      {items.map((component) => (
        <li key={component.id}>
          <span>{component.id}</span>
          <small>
            {component.item_type_id} · {component.quantity || 1} шт.
          </small>
          {component.children?.length ? (
            <Components items={component.children} />
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ItemDetail({
  id,
  token,
  onClose,
}: {
  id: string;
  token: string;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const item = useQuery({
    queryKey: ["item", id, token],
    queryFn: () => api.item(id, token),
  });
  const timeline = useQuery({
    queryKey: ["timeline", id, token],
    queryFn: () => api.timeline(id, token),
  });
  const [reason, setReason] = useState("");
  const [disposition, setDisposition] = useState<
    "rework" | "rejected" | "released"
  >("rework");
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const mutation = useMutation({
    mutationFn: () =>
      api.disposition(
        id,
        {
          disposition,
          reason: reason.trim(),
          expected_revision: item.data!.revision,
          idempotency_key: requestKey,
        },
        token,
      ),
    onSuccess: () => {
      client.invalidateQueries();
      setReason("");
      setRequestKey(crypto.randomUUID());
    },
  });
  return (
    <div className="detail">
      <button className="back" onClick={onClose}>
        ← Все изделия
      </button>
      <h2>Паспорт {id}</h2>
      <ErrorBox error={item.error || timeline.error || mutation.error} />
      {item.data && (
        <>
          <div className="grid three">
            <div className="panel">
              <span className="eyebrow">Тип / задание</span>
              <strong>{item.data.item_type_id}</strong>
              <p>{item.data.work_order_id}</p>
            </div>
            <div className="panel">
              <span className="eyebrow">Итог изделия</span>
              <Chip value={item.data.disposition} />
              {item.data.review_required && (
                <p className="warning">
                  Нужен повторный просмотр: появились новые основания.
                </p>
              )}
            </div>
            <div className="panel">
              <span className="eyebrow">Версия сборки</span>
              <strong>{human(item.data.assembly_version)}</strong>
            </div>
          </div>
          <div className="grid two">
            <section className="panel">
              <h3>Состав</h3>
              <Components items={item.data.components} />
            </section>
            <section className="panel">
              <h3>Решение по изделию</h3>
              <p className="muted">
                Отдельно от признаков и решений по дефектам.
              </p>
              <div className="formline">
                <select
                  value={disposition}
                  onChange={(e) => {
                    setDisposition(e.target.value as typeof disposition);
                    setRequestKey(crypto.randomUUID());
                  }}
                >
                  <option value="rework">Доработка</option>
                  <option value="rejected">Отклонить</option>
                  <option value="released">Выпустить</option>
                </select>
                <input
                  value={reason}
                  onChange={(e) => {
                    setReason(e.target.value);
                    setRequestKey(crypto.randomUUID());
                  }}
                  placeholder="Основание решения"
                />
              </div>
              <button
                disabled={!reason.trim() || mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                Сохранить решение
              </button>
            </section>
          </div>
        </>
      )}
      <section className="panel">
        <h3>История</h3>
        <p className="muted">
          Время события и время приёма показаны раздельно. Порядок по времени не
          доказывает причину.
        </p>
        {timeline.isPending && <p>Загрузка…</p>}
        {timeline.data?.items.length ? (
          <div className="timeline">
            {timeline.data.items.map((entry) => (
              <article key={entry.id}>
                <div className="time">
                  {when(entry.occurred_at)}
                  <small>Принято: {when(entry.received_at)}</small>
                </div>
                <div>
                  <strong>{entry.summary}</strong>
                  <p>
                    {entry.kind} · {human(entry.projection_status)}
                  </p>
                  {entry.duration && (
                    <small>
                      Длительность:{" "}
                      {entry.duration.value == null
                        ? human(entry.duration.unavailable_reason)
                        : `${entry.duration.value} ${entry.duration.unit}`}{" "}
                      · {entry.duration.kind} · {entry.duration.origin}
                    </small>
                  )}
                  {entry.source_event?.event_type === "inspection_observed" && (
                    <small>
                      Исходный сигнал: {entry.source_event.event_type}; медиа:{" "}
                      {"evidence_refs" in entry.source_event.payload &&
                      entry.source_event.payload.evidence_refs?.length
                        ? "ссылка передана"
                        : "не переданы"}
                    </small>
                  )}
                </div>
              </article>
            ))}
          </div>
        ) : (
          <Empty>События пока не получены.</Empty>
        )}
      </section>
    </div>
  );
}

function ReviewCard({ card, token }: { card: Nonconformity; token: string }) {
  const client = useQueryClient();
  const [decision, setDecision] = useState<
    "confirm" | "reject" | "request_check"
  >("request_check");
  const [reason, setReason] = useState("");
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const mutation = useMutation({
    mutationFn: () =>
      api.decide(
        card.id,
        {
          decision,
          reason: reason.trim(),
          expected_revision: card.revision,
          idempotency_key: requestKey,
          cause_status: "unresolved",
          cause_category: "unknown",
          evidence_event_ids: [],
        },
        token,
      ),
    onSuccess: () => {
      client.invalidateQueries();
      setReason("");
      setRequestKey(crypto.randomUUID());
    },
  });
  return (
    <article className="panel card">
      <div className="cardhead">
        <div>
          <span className="eyebrow">Карточка {card.id}</span>
          <h3>{card.item_id}</h3>
        </div>
        <Chip value={card.status} />
      </div>
      {card.review_required && (
        <p className="warning">
          Получены новые основания. Требуется пересмотр.
        </p>
      )}
      <div className="columns">
        <div>
          <h4>Исходные сигналы</h4>
          {card.observations.length ? (
            card.observations.map((o) => (
              <div className="evidence" key={o.id}>
                <strong>{when(o.occurred_at)}</strong>
                <p>
                  {human(o.original_result)} · качество:{" "}
                  {human(o.observation_quality)}
                </p>
                <small>
                  Эффективный результат: {human(o.effective_result)} ·{" "}
                  {o.defects.map((d) => d.type).join(", ") || "без признаков"}
                </small>
                <small>
                  Медиа:{" "}
                  {o.evidence_refs.length
                    ? `${o.evidence_refs.length} ссылок`
                    : "не переданы"}
                </small>
              </div>
            ))
          ) : (
            <p className="muted">Наблюдений нет.</p>
          )}
        </div>
        <div>
          <h4>Автоматический вывод</h4>
          <p>
            <Chip value={card.cause.status} /> {human(card.cause.category)}
          </p>
          <ul>
            {card.cause.limitations.map((limit, i) => (
              <li key={i}>{limit}</li>
            ))}
          </ul>
          {!card.cause.limitations.length && (
            <p className="muted">Ограничения не указаны.</p>
          )}
          <h4>Решения контролёра</h4>
          {card.decisions.length ? (
            card.decisions.map((d) => (
              <p key={d.id} className="decision">
                {human(d.decision)} · {d.author_id} · {when(d.decided_at)}
                <small>{d.reason}</small>
              </p>
            ))
          ) : (
            <p className="muted">Решений ещё нет.</p>
          )}
        </div>
      </div>
      <div className="decisionform">
        <select
          value={decision}
          onChange={(e) => {
            setDecision(e.target.value as typeof decision);
            setRequestKey(crypto.randomUUID());
          }}
        >
          <option value="request_check">Запросить проверку</option>
          <option value="confirm">Подтвердить дефект</option>
          <option value="reject">Отклонить сигнал</option>
        </select>
        <input
          value={reason}
          onChange={(e) => {
            setReason(e.target.value);
            setRequestKey(crypto.randomUUID());
          }}
          placeholder="Обязательное основание"
        />
        <button
          disabled={!reason.trim() || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          Записать решение
        </button>
      </div>
      <ErrorBox error={mutation.error} />
    </article>
  );
}

export default function App() {
  const [view, setView] = useState<View>("line");
  const [selected, setSelected] = useState<string | null>(null);
  const [token, setToken] = useState(
    () => sessionStorage.getItem("api-token") || "",
  );
  const orders = useQuery({
    queryKey: ["orders", token],
    queryFn: () => api.workOrders(token),
  });
  const items = useQuery({
    queryKey: ["items", token],
    queryFn: () => api.items(token),
  });
  const cards = useQuery({
    queryKey: ["cards", token],
    queryFn: () => api.nonconformities(token),
  });
  const analytics = useQuery({
    queryKey: ["analytics", token],
    queryFn: () => api.analytics(token),
  });
  const deliveries = useQuery({
    queryKey: ["deliveries", token],
    queryFn: () => api.deliveries(token),
  });
  const line = useQuery({
    queryKey: ["line", token],
    queryFn: () => api.line("line-1", token),
    enabled: view === "line",
  });
  const openItem = (id: string) => {
    setSelected(id);
    setView("items");
  };
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brandmark">⌁</span>
          <div>
            <strong>Контур качества</strong>
            <small>Демонстрационный MVP</small>
          </div>
        </div>
        <nav>
          {(Object.keys(labels) as View[]).map((key) => (
            <button
              className={view === key ? "active" : ""}
              key={key}
              onClick={() => {
                setView(key);
                setSelected(null);
              }}
            >
              {labels[key]}
            </button>
          ))}
        </nav>
        <div className="sidebarbottom">
          <span className="eyebrow">Доступ к API</span>
          <input
            type="password"
            aria-label="Токен API"
            placeholder="Bearer token"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              sessionStorage.setItem("api-token", e.target.value);
            }}
          />
          <small>Токен хранится только в текущей вкладке.</small>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className="eyebrow">FINAL HACK / КОНТРОЛЬ КАЧЕСТВА</span>
            <h1>
              {selected && view === "items" ? "История изделия" : labels[view]}
            </h1>
          </div>
          <span className="date">
            Данные из backend · обновление каждые 10 с
          </span>
        </header>
        {view === "line" && (
          <>
            <div className="grid three">
              <div className="stat">
                <span>Изделий проверено</span>
                <strong>{analytics.data?.inspected_items ?? "—"}</strong>
              </div>
              <div className="stat">
                <span>Изделий с дефектом</span>
                <strong>
                  {analytics.data?.confirmed_nonconforming_items ?? "—"}
                </strong>
              </div>
              <div className="stat">
                <span>Подтверждённых дефектов</span>
                <strong>{analytics.data?.confirmed_defects ?? "—"}</strong>
              </div>
            </div>
            <ErrorBox error={analytics.error || line.error || orders.error} />
            <div className="grid two">
              <section className="panel">
                <h3>Состояние линии</h3>
                {line.data ? (
                  <>
                    <p>
                      Линия {line.data.line_id} · {when(line.data.computed_at)}
                    </p>
                    <div className="rows">
                      {Object.entries(line.data.station_states).map(
                        ([id, state]) => (
                          <div key={id}>
                            <span>{id}</span>
                            <Chip value={state} />
                          </div>
                        ),
                      )}
                    </div>
                    <p>
                      Активные операции:{" "}
                      {line.data.active_operations.join(", ") || "нет"}
                    </p>
                  </>
                ) : (
                  <Empty>Нет состояния линии.</Empty>
                )}
              </section>
              <section className="panel">
                <h3>Полученные задания</h3>
                {orders.data?.items.length ? (
                  <div className="rows">
                    {orders.data.items.map((o) => (
                      <div key={o.id}>
                        <span>
                          {o.external_order_id}
                          <small>
                            {o.item_ids.length} изделий · маршрут{" "}
                            {o.route_version}
                          </small>
                        </span>
                        <Chip value={o.import_status} />
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty>Задания не получены.</Empty>
                )}
              </section>
            </div>
          </>
        )}
        {view === "items" &&
          (selected ? (
            <ItemDetail
              id={selected}
              token={token}
              onClose={() => setSelected(null)}
            />
          ) : (
            <>
              <ErrorBox error={items.error} />
              <section className="panel">
                <h3>Изделия</h3>
                {items.data?.items.length ? (
                  <div className="rows clickable">
                    {items.data.items.map((i) => (
                      <button key={i.id} onClick={() => openItem(i.id)}>
                        <span>
                          <strong>{i.id}</strong>
                          <small>
                            {i.item_type_id} · {i.work_order_id}
                          </small>
                        </span>
                        <Chip value={i.disposition} />
                      </button>
                    ))}
                  </div>
                ) : (
                  <Empty>Изделия не получены.</Empty>
                )}
              </section>
            </>
          ))}
        {view === "review" && (
          <>
            <ErrorBox error={cards.error} />
            {cards.data?.items.length ? (
              cards.data.items.map((c) => (
                <ReviewCard key={c.id} card={c} token={token} />
              ))
            ) : (
              <Empty>Очередь рассмотрения пуста.</Empty>
            )}
          </>
        )}
        {view === "analytics" && (
          <>
            <ErrorBox error={analytics.error} />
            {analytics.data ? (
              <>
                <p className="muted">
                  Серверный расчёт {when(analytics.data.computed_at)} · ревизия{" "}
                  {analytics.data.revision}
                </p>
                <div className="grid three">
                  {(
                    [
                      ["Проверено изделий", analytics.data.inspected_items],
                      [
                        "Изделий с несоответствием",
                        analytics.data.confirmed_nonconforming_items,
                      ],
                      [
                        "Подтверждённых дефектов",
                        analytics.data.confirmed_defects,
                      ],
                      [
                        "Кандидатов на проверку",
                        analytics.data.pending_candidates,
                      ],
                      [
                        "Подтверждённых ошибок оператора",
                        analytics.data.confirmed_operator_errors,
                      ],
                      ["Повторных операций", analytics.data.rework_operations],
                    ] as const
                  ).map(([label, value]) => (
                    <div className="stat" key={label}>
                      <span>{label}</span>
                      <strong>{value}</strong>
                    </div>
                  ))}
                </div>
                <section className="panel">
                  <h3>Длительности</h3>
                  {analytics.data.durations.length ? (
                    <div className="rows">
                      {analytics.data.durations.map((d, i) => (
                        <div key={i}>
                          <span>
                            {Object.values(d.group).join(" / ") || "Все"}
                            <small>
                              {d.kind} · {d.origin} · завершено{" "}
                              {d.completed_count}, незавершено{" "}
                              {d.incomplete_count}, некорректно{" "}
                              {d.invalid_count}
                            </small>
                          </span>
                          <strong>
                            {d.mean == null
                              ? "Неизвестно"
                              : `${d.mean.toFixed(1)} ${d.unit}`}
                          </strong>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Empty>Измерений пока нет.</Empty>
                  )}
                </section>
              </>
            ) : (
              <Empty>Аналитика недоступна.</Empty>
            )}
          </>
        )}
        {view === "exchange" && (
          <>
            <ErrorBox error={deliveries.error} />
            <section className="panel">
              <h3>Исходящие результаты</h3>
              <p className="muted">
                ACK означает подтверждение конкретного message_id внешним
                эмулятором.
              </p>
              {deliveries.data?.items.length ? (
                <div className="rows">
                  {deliveries.data.items.map((d) => (
                    <div key={d.message_id}>
                      <span>
                        <strong>{d.message_id}</strong>
                        <small>
                          Задание {d.external_order_id} · изделие {d.item_id} ·
                          версия {d.result_version} · попыток {d.attempts}
                        </small>
                        {d.last_error && (
                          <small className="warning">{d.last_error}</small>
                        )}
                      </span>
                      <Chip value={d.status} />
                    </div>
                  ))}
                </div>
              ) : (
                <Empty>Результаты не отправлялись.</Empty>
              )}
            </section>
          </>
        )}
        <footer>
          Синтетический сценарий. Признак, гипотеза, решение человека и итог
          изделия показаны отдельно.
        </footer>
      </main>
    </div>
  );
}
