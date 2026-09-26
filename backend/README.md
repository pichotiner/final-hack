# Backend: защищённый журнал и воспроизводимая история изделия

Текущий `app.main:app` обслуживает общий `/api/v2` по `contracts/openapi.yaml`
**0.4.0** поверх ядра P4. Контракт, ERP-эмулятор и UI перенесены из PR №6 роли №2
и исправлены в интеграции P5. Проверен A04 до persistent ERP ACK; описание,
границы и передача на review №2 — [P5](../docs/architecture/p5-shared-integration.md).
`app/api/contract.json` сохранён только для внутренней схемы ProductionContext
и явного legacy-входа `app.legacy_main:app` (draft P3).

Следующий изолированный браузерный прогон P6 и ссылки на скринкаст/проверенные
строки A01–A14 — в [docs/demo](../docs/demo/README.md) и
[приёмочном протоколе](../docs/architecture/p6-acceptance-review.md).

## Что работает

- Проверка общего конверта и строгий JSON: версии, тип события, обязательные поля, ID, время с часовым поясом, недопустимые числа и повторяющиеся ключи.
- Сравнение содержимого через RFC 8785 + SHA-256; исходные байты JSON сохраняются без потери неизвестных полей.
- Проверка соответствия `source_id` служебной учётной записи. HTTP bearer-аутентификация использует registry хешей токенов, загруженную из secret-файла.
- PostgreSQL: уникальность `(source_id, event_id)`, атомарная запись исходника и отдельного состояния обработки, корректная конкуренция при повторной доставке.
- Повтор возвращает ID и время первого приёма. Другое содержимое с тем же ключом даёт `EventConflict` и не переписывает исходник.
- Исходный JSON зашифрован AES-256-GCM. AEAD защищает также идентификаторы, времена, хеш, начальную квитанцию и метаданные криптографического пакета.
- Ключи внедряются извне через `AesGcmProtector`. Поддержаны версии ключей; тесты проверяют чтение старых записей после смены активного ключа и отказ при недоступном ключе.
- Триггеры блокируют UPDATE, DELETE и TRUNCATE журнала. Интеграционные тесты проверяют работу под ограниченной ролью БД и обнаружение изменения метаданных даже после обхода триггера владельцем БД.
- `PostgresTraceabilityJournal` строит историю из аутентифицированных исходников изделия. Блокировки ключа события и изделия выполняются в PostgreSQL; изменения исходника, состояний, текущей истории и её версии атомарны.
- `QualityWorkflow` реализует immutable basis/revision, stale rejection, confirm/rework/resolve/release, полноту и blockers, KPI, full-snapshot outbox и проверяемый ACK; `PostgresQualityWorkflow` сохраняет его под общим guard DP-01.
- `PostgresProductionJournal` соединяет raw, историю, quality context, B и outbox одной транзакцией под общим guard demonstrator. Legacy writers отклоняются для уже управляемого изделия.
- Выпуск проверяет версионированный производственный контекст, завершённые операции, допустимый источник/метод, цикл и качество наблюдения. Poor/reject, непривязанный сигнал и повтор старого цикла не обходят контроль.
- `ProductionService` назначает автора/источник из аутентифицированного principal и проверяет роли; ключ журнала и registry идентичностей загружаются fail-closed из обязательных файлов.
- HTTP: `app.main:app` публикует `/api/v2`, bearer/RBAC, проверяет запросы и реальные успешные ответы общей JSON Schema. Item/Card ссылаются на серверный B одной ревизии; повторное чтение не пересоздаёт B. Автор решения не принимается из JSON.
- `OutboxDeliveryWorker` арендует committed snapshot короткой PostgreSQL-транзакцией, отправляет его вне транзакции ядра, проверяет ACK через quality workflow и сохраняет ошибки/попытки.
- Поступление сохраняет тип изделия, ID задания, компоненты и версию сборки. Неизвестные типы компонентов и импортированное задание не выдумываются.
- Операции различают `in_progress`, `incomplete`, `completed`, `invalid`. Полное время рассчитывается отдельно от переданного источником активного времени; пропуск границы, сомнительные часы и конфликт дают `null` с причиной.
- Поздние факты перестраивают историю. Старые снимки append-only; первоначальная квитанция дубля остаётся прежней, текущее состояние обработки доступно отдельно. Повтор неизменного replay не создаёт новую версию.

## Быстрый запуск проверок (PowerShell)

Открыть терминал в каталоге `backend/`. Требуется Python 3.11. Для интеграционных проверок — работающий Docker Engine с Linux-контейнерами и Compose.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pytest -m "not integration"
.\.venv\Scripts\python.exe scripts/test_postgres.py
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pip check
```

Для запуска HTTP после применения миграций нужны обязательные `DATABASE_URL`,
`QC_EVENT_KEY_FILE`, `QC_EVENT_KEY_ID`, `QC_EVENT_KEY_VERSION` и
`QC_IDENTITIES_FILE`; секреты не имеют значений по умолчанию. Затем:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`GET /health/live`, `/health/ready` и OpenAPI не требуют учётной записи. Предметные маршруты используют
`Authorization: Bearer ...`; токен сопоставляется с SHA-256 хешем из registry.
`GET /openapi.json`, runtime-маршруты и DTO берутся из `contracts/openapi.yaml`.
Путь к каталогу можно задать `QC_CONTRACT_ROOT`; Compose монтирует его read-only.
RBAC задаёт сервер; условные предметные правила дополнительно проверяет ядро.

## Серверный Compose и готовый терминальный показ

Первый раз из `backend/` создайте локальные синтетические секреты:

```powershell
.\.venv\Scripts\python.exe -m app.manage init
```

Создаётся `.local/` в корне проекта (игнорируется git); существующий каталог
не перезаписывается. Там `compose.env`, ключ AES, registry хешей, файл demo-токенов
и отдельный исходящий ERP-токен. На Linux UID/GID приложения соответствует
пользователю, создавшему secret-файлы. Затем **из корня репозитория**:

```powershell
docker compose --env-file .local/compose.env -f deploy/compose.server.yaml up --build -d --wait
```

API: `http://127.0.0.1:8000`, схема: `/openapi.json`, интерактивная документация:
`/docs` (Swagger использует CDN, офлайн-показ — через скрипт ниже).
Миграции выполняются отдельно как `qc_admin`; сервер использует ограниченную
роль `qc_app`. Рабочий том dev-БД из `compose.dev.yaml` не затрагивается.

Старый `scripts/demo_server.py` рассчитан на **legacy P3**: для него отдельно
запускается `python -m uvicorn app.legacy_main:app`, а не общий P5 API.
Воспроизводимая проверка текущего полного A04 из `backend/`:

```powershell
.\.venv\Scripts\python.exe scripts/test_postgres.py tests/integration/test_erp_import_a04.py -q
```

Проверка поднимает изолированный PostgreSQL и живые TCP API/ERP. Вариант
`shared-ack` проверяет доработку/три повторных контроля/устранение/выпуск,
потерянный ACK, точный повтор, старые версии после финальной и ERP-сверку.
Тестовые контейнер/том удаляются; рабочая серверная БД сохраняется.
Старые `examples/server-demo*.json` остаются входами legacy-показа normal/late.

Для внешнего ERP роли №2 передайте ему токен из `.local/erp.token`, задайте
`QC_ERP_RESULTS_URL` (полный URL `/external/v2/quality-results`) и включите worker:

```powershell
docker compose --env-file .local/compose.env -f deploy/compose.server.yaml --profile erp up --build -d
```

При локальном Python-запуске worker нужны общие переменные backend плюс
`QC_ERP_RESULTS_URL` и `QC_ERP_TOKEN_FILE`:

```powershell
.\.venv\Scripts\python.exe -m app.worker
# Один due-пакет:
.\.venv\Scripts\python.exe -m app.worker --once
```

`GET /api/v2/integrations/deliveries` доступен integration/admin, показывает
попытки, lease, next_attempt_at и last_error. После исправления контрактной
ошибки: `POST /api/v2/integrations/deliveries/{message_id}/retry` с `{}`.
При отсутствии эмулятора outbox остаётся в ожидании, ACK не имитируется.

Остановка с сохранением БД и ключей:

```powershell
docker compose --env-file .local/compose.env -f deploy/compose.server.yaml --profile erp down
```

Запуск `scripts/test_postgres.py` создаёт отдельный Compose-проект со случайным именем, пароль только в окружении процесса и свободный локальный порт. Применяет настоящую миграцию через fixture, запускает интеграционные проверки и удаляет только свой контейнер, сеть и том. Рабочий dev-том команды не затрагивается. При первом запуске загружается образ PostgreSQL, закреплённый digest в `deploy/compose.dev.yaml`.

Для уже существующей тестовой БД можно передать `TEST_DATABASE_URL` и выполнить `python -m pytest -m integration`. Имя БД обязано оканчиваться на `_test`. Тестовая учётная запись должна уметь создавать схемы и роли: fixture создаёт изолированную схему и NOLOGIN-роль, затем удаляет их. Без переменной такие тесты помечаются как skipped, поэтому для полной проверки использовать Docker-скрипт.

**Текущий результат на Windows / Python 3.11.9 / PostgreSQL 17.9:** 160 тестов без БД и 68 на PostgreSQL, всего 228 backend. Контракт и ERP теперь находятся в этом репозитории; `QC_ROLE2_ROOT` не нужен (в контрольном прогоне явно указан текущий корень). Дополнительно 27 contract/ERP и 11 frontend tests, codegen/shape/build прошли. Ruff, форматирование и `pip check` проходят. Это не промышленная валидация или нагрузочное испытание.

Предыдущий срез журнала содержал 50 тестов (37 + 13); все они включены в текущий прогон. Интеграционный запуск применяет шесть миграций в изолированной PostgreSQL и проверяет соответствие схемы модели хранения; созданные им контейнер, сеть и том удаляются после тестов.

## Импорт задания ERP по HTTP (P4)

`app.import_erp` читает `/external/v2/work-orders`, проверяет переданную внешнюю
OpenAPI роли №2 и отображает заказ через явный профиль. Все изделия одного заказа,
B/outbox и первоначальная квитанция сохраняются одной транзакцией. Повтор безопасен
после перезапуска и после частичного прогона нескольких заказов. Подробности,
результаты и открытые транспортные вопросы — [P4](../docs/architecture/p4-erp-import.md).

Нужны применённая миграция `0006_erp_imports`, обычные переменные runtime из раздела
запуска HTTP, файл ERP-токена и отдельный файл bearer учётной записи с ролью
`integration` из `QC_IDENTITIES_FILE`. Пример ниже предполагает, что её токен
подготовлен локально в `.local/import.token` (в git не добавляется).
`contracts/` и `emulator/` теперь включены в эту поставку. `QC_ROLE2_ROOT` нужен
только для адресного теста другого checkout. Для запуска CLI из текущего дерева
используйте `--contract ../contracts/external-openapi.yaml`.
Команды из `backend/`:

```powershell
.\.venv\Scripts\python.exe -m app.import_erp --url http://127.0.0.1:8090/external/v2/work-orders --contract ../contracts/external-openapi.yaml --profile examples/erp-import-profile.json --erp-token-file ../.local/erp.token --integration-token-file ../.local/import.token
# Изолированная проверка на настоящем эмуляторе, Uvicorn и PostgreSQL:
.\.venv\Scripts\python.exe scripts/test_postgres.py tests/integration/test_erp_import.py tests/integration/test_erp_import_a04.py -q
```

Профиль примера версии **2**, `erp_result_profile=dp01-0.4`, покрывает только `order-001`, маршрут `route-bracket@1` и три
демонстрационные сборки. Для другого заказа требуется закрепить его ID и связи
в отдельном профиле; несовпадение не угадывается. Импорт сохраняет план и не создаёт
факты поступления, установки компонентов или завершения операций.

Каждая строка результата относится к закоммиченному заказу; финальная строка
`completed=true` означает завершение чтения всех страниц. При ошибке CLI возвращает
exit code 1, `completed=false` и безопасный код ошибки; можно повторить команду
с теми же документами и профилем. HTTP timeout/429/5xx не считаются успешным импортом.

Без объединённого `contracts/` или `QC_ROLE2_ROOT` проверки ERP помечаются skipped.
Явно заданный неправильный `QC_ROLE2_ROOT` приводит к ошибке теста.
В проверенном A04 дефект сохранён, устранён, открытых дефектов 0, изделие released,
**финальный результат сохранён ERP и ACK проверен**. Старый draft-stream нельзя
переключить на новый профиль: используйте новую БД/stream для demonstrator;
существующие сообщения, ID и digest не переписываются.

## Локальный эксперимент стоимости истории

Из каталога `backend/`, в установленном окружении:

```powershell
.\.venv\Scripts\python.exe scripts/measure_replay.py --operations 50 250 1000 --repeats 7 --growth-operations 20 40 80
```

Скрипт сравнивает обычные операции и длинную цепочку доработок: время reducer, время подготовки JSON, размер последнего снимка и сумму полных версий. Синтетические входы строятся в памяти, проверяются длительности и независимость результата от порядка доставки. БД и Docker не требуются. Для автоматического сохранения добавить `--output <новый-путь.json>` в существующем каталоге: UTF-8, без перезаписи старого результата.

Методика, два сохранённых отчёта, границы и предложение масштабирования — [scaling.md](../docs/architecture/scaling.md). Это не замер приёма/SQL/шифрования, размера БД или нагрузки всей системы.

## Терминальный эксперимент «радиус проверки»

Из `backend/`:

```powershell
.\.venv\Scripts\python.exe scripts/impact_experiment.py --case late-component
.\.venv\Scripts\python.exe scripts/impact_experiment.py --case removed-component
.\.venv\Scripts\python.exe scripts/impact_experiment.py --case missing-completeness
.\.venv\Scripts\python.exe -m pytest tests/test_impact_experiment.py
```

Показывает путь сигнала компонента к сборке, прежнюю сборку после снятия и открытый охват при неизвестной полноте. `--json` выводит структурированные объяснения. **25 тестов прошли**, включая 384 сравнения с независимым полным перебором. Входы `scripts/impact_inputs.json` и ожидания `tests/impact_expected.json` разделены.

Это отдельная синтетическая модель в памяти; к приёму событий/БД, решениям, выпуску и ERP она не подключена. Методика и точная граница доказательства — [impact-experiment.md](../docs/architecture/impact-experiment.md).

## Миграции и отдельная dev-БД

Из корня репозитория, после задания локального `POSTGRES_PASSWORD` в окружении:

```powershell
docker compose -f deploy/compose.dev.yaml up -d --wait
```

По умолчанию PostgreSQL доступен только на `127.0.0.1:55432`, пользователь миграций `qc_admin`, БД `final_hack`; порт можно изменить через `POSTGRES_PORT`. Пароль не имеет значения по умолчанию и не хранится в git. Для повторного использования dev-тома нужен пароль, выбранный при его создании.

В каталоге `backend/` задать `DATABASE_URL` из своего окружения (формат `postgresql+psycopg://USER:PASSWORD@HOST:PORT/DB`) и выполнить:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
```

Этот Compose запускает только dev-БД. Полный серверный запуск с миграциями и ограниченной учётной записью `qc_app` — отдельный `compose.server.yaml`, описанный выше. UI и persistent ERP включены из поставки №2; запускаются отдельно (`npm run dev` в frontend и `python -m emulator.server` из корня).

Миграция `0001_event_journal` создаёт журнал, `0002_item_traceability` — версии истории, `0003_quality_workflow` — B/решения/outbox, `0004_production_context` — версии контекста и ограничения области, `0005_delivery_leases` — устойчивые попытки/аренды доставки, `0006_erp_imports` — исходные задания ERP, профили и квитанции импорта. Downgrade с удалением доказательной истории намеренно не выполняется.

## Подключение внутренней истории

Ниже показан legacy-эксперимент истории. Quality-managed demonstrator обязан использовать `PostgresProductionJournal`; смешивать writers запрещено. Актуальная внутренняя серверная граница описана в [P2](../docs/architecture/p2-integrated-quality-core.md).

```python
from app.adapters.json_events import JsonEventCodec
from app.adapters.traceability import PostgresTraceabilityJournal
from app.application.ingestion import IngestEvent

# engine: SQLAlchemy PostgreSQL Engine, READ COMMITTED.
# protector: AesGcmProtector с ключами, полученными из внешней конфигурации.
journal = PostgresTraceabilityJournal(engine, protector)
ingest = IngestEvent(journal, protector, JsonEventCodec())

# HTTP-адаптер должен предварительно аутентифицировать источник и проверить DTO.
receipt = ingest.execute(raw_json_bytes, authenticated_source_id=source_id)
current = journal.get_history(item_id)
previous = journal.get_history(item_id, revision=1)
verified = journal.replay(item_id)
```

Это пример композиции, не самостоятельный сервер. Чтение возвращает JSON-совместимый словарь с `revision`, `computed_at`, `processor_version`, `timeline`, `operations` и ссылками на исходники. Отсутствующая история — `None`. В `operations[].durations` первое значение — вычисленный `station_elapsed`, затем отдельные переданные измерения со ссылками на факты. Тело наблюдения не копируется в открытую историю, оно остаётся в защищённом исходнике.

Для ограниченной роли БД нужны SELECT/INSERT для `raw_events`, SELECT/INSERT/UPDATE для `event_projections` и `item_projections`, SELECT/INSERT для `item_projection_versions`; fixture интеграционных тестов проверяет именно такой набор. Владение таблицами приложению не требуется. Не смешивать legacy `PostgresEventJournal` и новый адаптер при конкурентной записи одного изделия. Уже сохранённые legacy-факты с корректным типовым содержимым можно обработать через `replay(item_id)`.

Предметные ограничения и результат проверки контракта второго участника — [backend-traceability-design.md](../docs/backend-traceability-design.md).

## Структура

```text
app/domain/                 типы событий, квитанций и ошибок
app/domain/control.py       закреплённый производственный контекст и план контроля
app/domain/traceability.py   факты, операции, длительности и детерминированный replay
app/ports/                  интерфейсы журнала, сериализации и защиты
app/application/ingestion.py внутренний сценарий приёма
app/application/production.py аутентифицированные команды и права
app/application/bootstrap.py fail-closed конфигурация БД, ключа и identity registry
app/adapters/json_events.py  общий конверт, исходный JSON и канонизация
app/adapters/protection.py   AES-GCM, метаданные пакета и версии ключей
app/adapters/postgres.py     PostgreSQL-журнал и атомарная запись состояния
app/adapters/traceability.py блокировки, проверка исходников и версии истории
app/adapters/production.py  общая raw → history → quality → B → outbox транзакция
migrations/                 Alembic и append-only ограничения
tests/                      предметные и PostgreSQL-проверки
scripts/test_postgres.py     изолированный воспроизводимый запуск тестов БД
scripts/measure_replay.py    локальный замер reducer и JSON-версий истории
scripts/impact_experiment.py синтетический поиск влияния по составу с объяснениями
```

## Граница с работой роли №2

Сервис `IngestEvent.execute(raw, authenticated_source_id=...)` — внутренняя точка подключения. HTTP-адаптер сначала проверяет права, строгий JSON и общую схему; `validate_source_payload` проверяет payload конкретного типа. Source identity и автор берутся из registry. TypeScript/Pydantic регенерированы из 0.4.0; сервер валидирует исходное тело JSON Schema и не пересериализует raw через DTO. Историческая карта P3 — [здесь](../docs/architecture/p3-server-integration.md), текущая — [P5](../docs/architecture/p5-shared-integration.md).

`received_at` присваивается сервисом; одноимённое поле во входном JSON отклоняется. Неподдерживаемые типы, включая `controller_decision`, не принимаются как события источника. Авторизованная композиция `ProductionService` вызывается из `app/api/server.py`.

P0/P1-срез решений и выпуска описан в [p0-quality-slice.md](../docs/architecture/p0-quality-slice.md) и [p1-quality-acceptance.md](../docs/architecture/p1-quality-acceptance.md). Роль №1 владеет сервером и правами; роль №2 поставляет общую OpenAPI/generated DTO, UI и внешний эмулятор.

## Текущие ограничения

- Legacy-адаптер `PostgresEventJournal` по-прежнему оставляет `domain_projection_not_implemented`. Актуальный HTTP использует `PostgresProductionJournal`; двусторонний обмен 0.4.0 проверен в A04. Совместное review корректирующей схемы №2 остаётся открытым.
- Компоненты пока представлены ID из поступления, без типа из справочника и вложенного дерева. Неоднозначные/отсутствующие паспорта явно отмечены. Связи операций проверяются внутри изделия; глобальная проверка одинакового operation_run_id между изделиями ещё предстоит.
- Legacy-адаптеры не являются полным предметным обработчиком. HTTP использует интегрированный путь `PostgresProductionJournal`.
- Снимки истории и их версии содержат открытые метаданные и не защищены AEAD. Чтение снимка не проверяет заново исходники; `replay` проверяет использованные исходники. Владелец БД может обойти триггеры: полноту выборки при удалении/перепривязке фактов нужно защищать внешними контрольными точками.
- Replay читает все надёжно привязанные факты изделия; некорректное содержимое legacy-факта или недоступный старый ключ блокирует пересчёт с откатом. Масштабирование пересчёта и хранения полных версий нужно измерять отдельно.
- Общая спецификация/codegen, UI и persistent ERP работают в границе P5; полный browser E2E/скринкаст, остальные A01–A14, TLS deployment, эксплуатационный identity lifecycle и restore-процедура ещё не закрыты.
- AEAD проверяет отдельную запись, не полноту журнала: владелец БД может отключить триггеры и удалить запись. Необходимые границы защиты, внешние контрольные точки и восстановление предстоит проработать отдельно. Изменение содержимого/привязки записи обнаруживается при проверке AEAD.
- Идентификаторы, времена и канонический хеш доступны в индексируемых метаданных. Исходный JSON хранится зашифрованным. Метаданные проекций пока не зашифрованы.
- RFC 8785 ограничивает числовой диапазон. Время нормализуется к UTC и точности PostgreSQL/Python в микросекундах; исходная строка остаётся в зашифрованном JSON. Leap second не поддерживается текущим парсером.
- Проверка восьми конкурентных отправок доказывает идемпотентность этого сценария, но не является проверкой горизонтального масштабирования всего MVP.

## Обновление lock-файла

`requirements.lock.txt` содержит runtime- и test-зависимости, получен pip-tools 7.5.3 на Python 3.11. Для регенерации в отдельном окружении инструментов:

```powershell
python -m pip install pip-tools==7.5.3 setuptools==82.0.1
python -m piptools compile --extra test --no-build-isolation --strip-extras --output-file requirements.lock.txt pyproject.toml
```

После изменения зависимостей повторить установку и проверки. Lock-файл не исправлять вручную.
