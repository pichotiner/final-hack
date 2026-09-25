# План команды — architecture first

Дата: 25 сентября 2026 года.

После консультации с экспертом план изменён. Главный результат ближайшего этапа — **не кодовая база, а защищаемая архитектура**.

Ближайшая последовательность, владельцы и глубина критериев уточнены в [architecture-rework-plan.md](architecture-rework-plan.md). Стартовые схема, пример и методы внедрения — в [architecture-design-brief.md](architecture-design-brief.md). Этапы ниже — расширенный backlog; первая поставка каждой роли указана в начале [roles.md](roles.md).

Ранние решения по FastAPI/PostgreSQL/React остаются возможной реализационной гипотезой, но до архитектурного freeze они не должны диктовать устройство системы.

Основные документы:

- [architecture-hypothesis.md](architecture-hypothesis.md) — выбранное направление;
- [roles.md](roles.md) — конкретные задачи двух ролей;
- [requirements.md](requirements.md) — требования и критерии;
- [contracts.md](contracts.md) — текущая семантика событий;
- [demo.md](demo.md) — сценарии, которые потом должен доказать MVP.

---

## Этап A0 — перечитать кейс как архитекторы

Обе роли должны выписать не функции интерфейса, а **архитектурные силы**:

- закрытое производство;
- ограниченные реальные данные;
- несколько разнородных источников;
- разные уровни доверия;
- задержки и повторы;
- необходимость доказуемой истории;
- решения человека;
- потенциальная смена ERP/MES/CAD;
- развитие к Kafka/OpenTelemetry;
- защита и аудит.

### Результат

Один общий лист:

```text
Constraint
→ Architectural consequence
→ What can fail
→ How we prove it
```

---

## Этап A1 — альтернативы

### Роль №1

Сравнивает:
- CRUD/state-centric с полноценным аудитом и версионированием;
- Event Sourcing;
- Event Sourcing + Evidence Graph;
- отдельно, как независимую ось, deployment и транспорт: модульное ядро/microservices, HTTP/outbox/broker.

### Роль №2

Сравнивает:
- central CV;
- edge CV;
- hybrid edge/central;
- варианты интеграции напрямую vs adapters;
- подходы к offline buffering.

### Общее правило

У каждой альтернативы должны быть:
- плюсы;
- минусы;
- эксплуатационная цена;
- ограничения закрытого производства;
- влияние на доказуемость;
- влияние на MVP.

### Результат

ADR и таблица trade-offs.

---

## Этап A2 — спроектировать EFQA

Зафиксировать:

### Evidence plane
- source identity;
- provenance;
- immutable raw evidence;
- occurred_at / received_at;
- analyzer version;
- confidence / quality;
- tamper detection.

### Context plane
- Product Digital Thread;
- component structure;
- operations;
- equipment;
- operators/actions.

### Evidence Graph
- фактические связи;
- вычисленные связи;
- гипотезы;
- противоречащие основания.

### Decision plane
- controller review;
- disposition;
- author/reason;
- revision;
- review_required.

### Integration plane
- source adapters;
- ERP/MES/CAD adapters;
- outbox/ACK concept;
- source-of-truth matrix.

### Edge plane
- local acquisition;
- optional CV;
- store-and-forward;
- recovery;
- source clock uncertainty.

---

## Этап A3 — отказовые сценарии

Нарисовать sequence diagram минимум для:

1. нормального изготовления;
2. дефекта после операции;
3. duplicate;
4. late event;
5. central link down;
6. ERP down;
7. not_assessable;
8. analyzer v2;
9. tamper attempt.

Для каждого ответить:

- что записано навсегда;
- что можно пересчитать;
- что видит человек;
- что будет отправлено наружу;
- какой статус неопределённости.

---

## Этап A4 — architecture freeze

Кодовый этап не начинается, пока нет:

- [ ] system-context;
- [ ] component/container diagram;
- [ ] edge/on-prem deployment;
- [ ] Evidence Graph meta-model;
- [ ] ADR альтернатив;
- [ ] replay sequence;
- [ ] offline/store-and-forward sequence;
- [ ] source-of-truth matrix;
- [ ] trust/threat model;
- [ ] AI observation contract;
- [ ] adaptation/MLOps loop;
- [ ] scaling model;
- [ ] bottlenecks;
- [ ] 30 sec pitch;
- [ ] 3 min architecture explanation;
- [ ] список проектных предположений.

На freeze оба участника должны уметь независимо объяснить всю схему.

---

## Этап M0 — выбрать минимальное доказательство

Только после A4.

Нужно выбрать **небольшую систему**, которая докажет архитектуру.

Рекомендуемая граница:

```text
ERP emulator
→ work order
→ item
→ operation 1
→ inspection
→ operation 2
→ defect observation
→ Evidence Graph
→ controller decision
→ result
→ ACK
```

Добавить:
- duplicate;
- late event;
- analyzer_version;
- по возможности edge queue.

Не добавлять функции, которые не доказывают архитектурное свойство.

---

## Этап M1 — маленький AI

AI не является отдельным большим треком.

Минимум:

- 1 объект;
- 1–2 дефекта или anomaly;
- confidence;
- observation_quality;
- analyzer_version;
- стабильный контракт.

Допускается:
- простая готовая модель;
- классический CV;
- небольшой обученный detector;
- полностью эмулированный анализ.

Выбор зависит от того, что быстрее доказывает **replaceability of analyzer**.

---

## Этап M2 — architecture proof MVP

Каждая демонстрация должна соответствовать архитектурному утверждению.

| Сценарий | Что доказываем |
| --- | --- |
| raw observation | provenance |
| duplicate | idempotency |
| late event | replay |
| controller decision | human/AI separation |
| two observations one defect | evidence linking |
| analyzer v2 | replaceable intelligence |
| edge reconnect | store-and-forward |
| result + ACK | reliable integration |
| tamper attempt | integrity |

---

## Этап M3 — только затем усиление

После работающего доказательства архитектуры:

- OpenAPI/code generation;
- CI;
- второй adapter;
- load test;
- multiple workers;
- OpenTelemetry;
- Kafka transport;
- extended crypto;
- polished UI;
- презентация и скринкаст.

Ни один из этих пунктов не должен ломать архитектурный нарратив ради дополнительных технологий.

---

# Распределение прямо сейчас

## Роль №1

Следующие действия по порядку:

1. Сделать ADR четырёх core-подходов.
2. Нарисовать Evidence Ledger lifecycle.
3. Сделать Evidence Graph meta-model.
4. Разобрать late-event replay.
5. Разобрать ordering/concurrency/scaling.
6. Нарисовать trust model.
7. Подготовить критические вопросы к собственной архитектуре.
8. Передать роль №2 стабильные semantic boundaries.

**Не писать backend**, кроме маленького эксперимента, если без него нельзя проверить спорное архитектурное свойство.

## Роль №2

Следующие действия по порядку:

1. Выбрать минимальный AI use case.
2. Зафиксировать observation contract.
3. Нарисовать camera/edge/on-prem deployment.
4. Сделать offline/store-and-forward model.
5. Сделать source-of-truth matrix.
6. Спроектировать 1 рабочий emulator adapter + схемы остальных интеграций.
7. Описать adaptation/MLOps для малых локальных данных.
8. Сравнить EFQA с типичными CV station / QMS / event platform.
9. Составить matrix «criterion → architecture → demo evidence».

**Не делать большой frontend**. Максимум — wireframe того, что нужно показать на защите.

---

# Ежечасная синхронизация

Каждая роль приносит не «что написано», а:

```text
1. Какое архитектурное решение приняли?
2. Какие альтернативы отбросили?
3. Почему?
4. Какой trade-off появился?
5. Как это решение ломается?
6. Как докажем его на демо?
7. Что должен знать второй участник?
```

Если ответов нет — работа ещё не закончена, даже если нарисована диаграмма или написан код.

---

# Стоп-сигналы

Остановиться и вернуться к архитектуре, если начинается:

- спор о CSS;
- большое количество API endpoints;
- обучение тяжёлой модели;
- Kubernetes;
- сложная очередь сообщений;
- оптимизация производительности без данных;
- «давайте добавим LLM» без архитектурной причины;
- выбор технологии до определения проблемы;
- попытка назвать систему уникальной только потому, что в ней несколько AI-модулей.

Главная цель: к защите команда должна выглядеть как люди, которые **спроектировали систему для реального закрытого производства**, а не как люди, которые за ночь написали ещё один dashboard.
