# P8 — завершение проверок доступа, чтения и защищённого пилота

Поручение владельца: реальная ротация credentials в браузере, SQL-пагинация
публичных списков, замеры ingestion/GET/ACK/storage/locks, TLS разнесённых
компонентов. Редактор — роль №1. Независимое review второй роли не заявляется.

## Решения до реализации

- Реальный браузер теряет ответ **после** commit; registry меняется штатной
  командой, API перезапускается. Старый token получает настоящий 401. Тот же actor
  с новым token повторяет прежние bytes/key и получает ровно одно решение.
- Публичные Page-списки ограничиваются в PostgreSQL (`limit + 1`): изделия,
  карточки, задания, доставки, timeline. Карточки выбираются по ключам JSONB,
  задания — последняя версия по паре system/order. Cursor задания становится
  непрозрачным составным ключом, клиент его не интерпретирует. Timeline закрепляет
  ревизию и использует SQL-порядок времени с tie-breakers. Внутренние полные
  выгрузки для restore и вычисление агрегатов не выдаются за страницы.
- Нагрузочный runner создаёт отдельный стенд и измеряет реальные HTTP/SQL пути,
  размеры JSON и физических relations, round-trip advisory-lock acquisition.
  Это синтетический замер, не SLA или доказательство промышленной ёмкости.
- Разнесённый профиль использует TLS с проверкой CA/имени на web→API,
  worker/importer→ERP и runtime→PostgreSQL. Порты приложений локальны внутри
  узла; reverse proxies публикуют только TLS. Bearer остаётся обязательным.
  Альтернатива — VPN/service mesh; цена выбранного решения — сертификаты,
  DNS и эксплуатация нескольких proxy. Локальная эмуляция узлов подтверждает
  протоколы; реальная сеть предприятия требует отдельной проверки.

Транспортные DTO не меняются. Пределы и cursor документируются в OpenAPI без
смены формы; действующие generated-потребители должны совпадать с источником.

## Результаты

База `23374e4` + рабочее дерево P8. Проверки выполнены 26–27 сентября 2026,
Windows/Python 3.11.9, Node 24, Docker Desktop/WSL2, PostgreSQL 17.9.

### 1. Настоящая ротация credentials

`backend/scripts/acceptance_demo.py` запускает живые API/PostgreSQL/ERP и Chromium.
Новый сценарий P8 выполняется последним, чтобы смена controller bearer не влияла
на предыдущие сценарии. Обрыв инжектируется только после настоящего HTTP 201:

1. Сервер записал release, браузер не получил ответ.
2. CLI `app.manage rotate-identity` реально заменил registry.
3. API перезапущен; новый bearer проверен через `/api/v2/session`.
4. Повтор старого bearer получил **401 от сервера**, не mock.
5. Другой actor (`master`) отвергнут формой восстановления.
6. Новый bearer прежнего actor → exact retry → **200**, исходный decision ID.
7. Все три отправки имели одинаковые bytes/key; число решений осталось **1**,
   context revision **10**, дополнительного предметного эффекта нет.

Прогон: `.local/p6-971067928e/evidence/results.json`, **19 PASS, failures=[]**.
Видео и `real-credential-rotation.png` находятся рядом. Изменение токена не
сохраняется в публикуемых HTTP-trace headers; секреты остаются в credentials.
Runner корректно завершает дочерние процессы Uvicorn на Windows при нескольких
workers. `--api-workers 2/4` предназначен для benchmark, не для ротационного E2E.

### 2. SQL-пагинация

`backend/app/adapters/read_pages.py` выполняет выбор `LIMIT size+1` в PostgreSQL
до передачи выбранных объектов Python. Item/card selection и построение DTO
происходят в одной `REPEATABLE READ` транзакции без writer/advisory lock.

| Endpoint | Порядок и консистентность |
| --- | --- |
| `/items` | keyset по item_id; DTO только выбранной страницы |
| `/nonconformities` | ключи карточек из `jsonb_object_keys`; DTO только выбранных карточек/изделий |
| `/work-orders` | `DISTINCT ON (system, order)` с последней revision; keyset по паре, непрозрачный cursor |
| `/integrations/deliveries` | keyset message_id; join outbox/lease, SQL LIMIT |
| `/items/{id}/timeline` | SQL JSONB expansion, сортировка timestamp/source/event/record; cursor item/revision/offset; late → 409, foreign/invalid → 422 |

Это **ограничение объёма ответа и Python-гидрации**, не обещание O(page-size)
работы БД: timeline всё ещё раскрывает/сортирует JSONB, выбор карточек обходит
JSONB-ключи. В пределах item detail полный B и история остаются частью DTO.
Остальные страницы — текущие снимки, а не замороженная многостраничная выгрузка.
Аналитика, статус линии и scope-review по определению вычисляют агрегат/полную
область решения и не маскируются под paginated Page. Legacy draft API не менялся.

Старые cursor timeline/order следует перезапросить с первой страницы. Это
документировано в OpenAPI. DTO и shape-baseline не изменились; Python/TS
регенерированы штатными генераторами. Frontend загружает все страницы заданий.

`tests/integration/test_read_pages.py`: bounded SQL, запрет прежних full-scan
методов, несколько страниц без дублей, latest order revision, late/foreign
cursor. Собственная схема теста нужна из-за отдельных encryption keys fixtures;
проверку AEAD при чтении не отключали.

### 3. Замеры полного пути и хранения

Воспроизведение из `backend/`, **последовательно**, без параллельной сборки Docker:

```powershell
.\.venv\Scripts\python.exe scripts/acceptance_demo.py --benchmark --api-workers 1
.\.venv\Scripts\python.exe scripts/acceptance_demo.py --benchmark --api-workers 2
.\.venv\Scripts\python.exe scripts/acceptance_demo.py --benchmark --api-workers 4
```

Runner `scripts/measure_pilot.py` измеряет TCP HTTP/Uvicorn, PostgreSQL и реальный
TCP ERP/SQLite ACK. В каждом новом стенде 21 импортированное изделие, одно горячее
изделие получает 11 исходных фактов и ещё 8/24/48 осмотров. Пул из 4 клиентов,
по 8 GET в фазе; ACK измеряется отдельной серией после приёма. Throughput — число
записей на время смешанной фазы с чтениями, не максимум одиночного writer.

Финальная фаза, накоплено **59 raw**, времена в миллисекундах:

| Процессов API | Ingest mean / p95 | GET mean / p95 | ACK mean / p95 | Записей/с |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 911.760 / 1997.547 | 242.140 / 423.293 | 40.639 / 65.001 | 3.880 |
| 2 | 890.046 / 2180.134 | 260.426 / 446.765 | 36.411 / 47.370 | 3.918 |
| 4 | 849.957 / 1386.248 | 188.881 / 263.047 | 46.132 / 60.967 | 4.305 |

1/2/4 прогоны: `.local/p6-6e45315721`, `.local/p6-01a3debd3b`,
`.local/p6-253ae86a73`; полный отчёт в `evidence/performance.json`.
Ранний 4-process прогон параллельно Docker build исключён из этой таблицы.

Рост хранения, 1-process прогон, **байты текстового JSON**:

| Raw | Текущее состояние всех 21 изделий | Все B | Все quality state versions |
| ---: | ---: | ---: | ---: |
| 11 | 82 618 | 382 921 | 241 000 |
| 19 | 95 562 | 828 617 | 427 056 |
| 35 | 121 478 | 2 357 101 | 1 110 034 |
| 59 | 160 358 | 6 243 421 | 2 912 098 |

При 59 raw: **151 B, 101 quality state versions, 80 outbox**; во всех трёх
прогонах одинаковые числа записей. Физический размер B (`pg_total_relation_size`,
с индексами/TOAST) — **1 605 632 байта**, state versions — **737 280 байт**,
raw — **163 840 байт** в 1-process прогоне. Это не размер JSON: компрессия,
страницы, индексы и мёртвые версии влияют отдельно. Все отчёты содержат также
`pg_column_size`; VACUUM перед замером не выполнялся.

ACK не меняет число/JSON/физический размер quality state versions — проверяется
assertion до/после каждой серии. Физический outbox может вырасти из-за UPDATE ACK;
утверждение «ACK вообще ничего не пишет» не делается.

**Блокировки:** sampler `pg_locks` каждые ~10 мс видел до **3 ожидающих writers**.
Samples with waiters / all: **682/1040**, **585/1008**, **555/947**. В отдельной
контролируемой проверке guard удерживался, writer действительно появился в
`pg_locks`, GET завершился **до освобождения** guard:

| API processes | GET при занятом guard | Время до освобождения guard | Полный задержанный ingest |
| ---: | ---: | ---: | ---: |
| 1 | 335.924 | 615.433 | 844.109 |
| 2 | 174.018 | 465.054 | 684.702 |
| 4 | 242.117 | 554.677 | 762.996 |

Sampling не даёт точного времени ожидания каждого запроса. В таблице время до
освобождения включает достижение guard, проверку GET и sleep; оно не выдается за
чистую SQL wait duration. Выборка небольшая, особенно p95 восьми GET; один прогон
на конфигурацию и hot-item сценарий не доказывают линейного масштабирования.

**Вывод:** отдельное хранение P7 убрало вложенные копии всех B и рост state от ACK,
но снимки B/версий всё ещё растут быстро. 4 API процесса дают лишь небольшой
выигрыш в этом hot-item workload. Следующее изменение — сокращение подготовки B/
replay и повторные замеры на нескольких активных изделиях. Ослаблять общий guard
без проверки транзакций scope/import нельзя.

### 4. TLS и четыре узла

Профили и инструкция: [deploy/multihost](../../deploy/multihost/README.md).
Реализованы индивидуальные leaf-ключи, per-host bundles без CA private key,
TLS web→API, importer/worker→ERP и PostgreSQL verify-full. Ядро в TLS-профиле
отклоняет plaintext ERP URL и PostgreSQL без verify-full/CA.

Проверено командами из корня:

```powershell
.\backend\.venv\Scripts\python.exe deploy/check_distributed_tls.py
.\backend\.venv\Scripts\python.exe deploy/check_multihost.py
.\backend\.venv\Scripts\python.exe deploy/check_pilot.py --distributed-tls
```

- `.local/p8-tls-bb25064414/tls-report.json`: CA/hostname API/ERP, ERP bearer,
  отказ unknown CA и wrong hostname, `pg_stat_ssl=true`, plaintext DB rejected;
  14 сообщений получили ACK.
- `.local/p8-hosts-531cc27b5b/report.json`: **4 отдельных Compose-проекта без
  общей сети**, трафик через опубликованные порты `host.docker.internal`, 11 raw,
  19 B, 14 ACK. Это эмуляция узлов на одном Docker Desktop, не WAN-испытание.
- `.local/p7-operations-563c6a2fc0/report.json`: populated TLS backup/restore,
  ключи [1,2], 12 raw, 23 B, 1 решение, 16 outbox, 19 versions;
  `redelivered=16`, fingerprints совпали. TLS-профиль сохраняется при restore.

Отрицательные trust-пробы запускаются только локальным тестовым модулем,
сертификаты не проверяются через `verify=False` или `--no-check-certificate`.
Внутри каждого узла proxy→API/ERP остаётся HTTP; это не межузловой канал.
Обычный pilot backup управляет одним Docker daemon; координация backup четырёх
физических узлов описана как эксплуатационная процедура, не автоматизирована.

### 5. Итоговые проверки

| Проверка | Результат |
| --- | --- |
| `python -m pytest -m "not integration"` | **171 passed** |
| `python scripts/test_postgres.py` | **77 passed** |
| Contract-venv `python -m pytest contracts emulator -q` | **27 passed** |
| Frontend `npm test` | **14 passed** |
| `npm run build`, `npm run check:generated` | Passed |
| `python -m contracts.generate --check` | Passed |
| Shape gates обеих OpenAPI | Без изменения DTO/shape |
| Backend/deploy Ruff + format | Passed |
| Chromium | **19 PASS**, failures=[] |

Всего **289 тестов**, отдельно браузер, три измерительных стенда, TLS и restore.
Следующий результат роли №1 — оптимизация B/replay по измерениям, роли №2 —
независимая проверка P8 на выбранных реальных узлах/DNS и документация их допуска.
Полный A01–A14, HA, промышленная сеть, реальные коннекторы и PQ не объявляются
закрытыми этой поставкой.
