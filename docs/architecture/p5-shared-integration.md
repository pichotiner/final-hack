# P5. Интеграция общего транспорта с ядром P4

26 сентября 2026. Проверочный эксперимент по поручению владельца проекта «делай»
после проверки поставки №2. Статус: реализовано локально и проверено в границе
ниже; совместный contract freeze не объявлен.

Передача результатов роли №2:
https://github.com/pichotiner/final-hack/pull/7#issuecomment-5847306257

## Решение до изменения потребителей

Проблема: существующие компоненты №2 и P4 имеют несовместимые DTO. Ожидание
ответа оставляет A04 незамкнутым; замена P4 на UnifiedChain теряет проверенные
контекст/guard/версии; выбираем перенос компонентов контракта/ERP/UI на P4.

Для этой интеграционной поставки роль №1 готовит корректирующую редакцию схем
на базе PR №6 и публикует её роли №2 на review. Это явная интеграционная поправка,
а не утверждение о полученном согласии №2. Generated меняются только генераторами.

- Публичный resolve принимает raw `control_record_ids`; внутри общего guard,
  после dedupe исходного тела, они отображаются в observation IDs. В решении
  сохраняются обе ссылки. Проверки типа/item/cycle/метода/области/времени остаются.
- CycleCommand сохраняет rework disposition и planned runs; добавляет явные
  checkpoint → session/run bindings (как уже проверено P4). Ответ возвращает
  серверный cycle ID и принятые bindings. UI вводит bindings явно, не угадывает.
- Basis передаёт неизменный analysis snapshot, наблюдения с индексом признака,
  исходные records/provenance, полноту и закреплённые версии. Чтение старого B
  использует его snapshot, не сегодняшние projections. Повторный GET текущего B
  не создаёт новые основания при неизменной revision/цели.
- QualityResult дополняется обязательным nullable disposition_decision с ID/B,
  автором, действием, причиной и reviewed revision. Resolved confirmed карточки
  остаются в полном результате. Внешнее тело формируется до digest и commit.
- Новый внешний формат включается закреплённым профилем контекста для новых
  streams. Старые draft сообщения/ID не меняются; смена формата занятого stream
  отклоняется. Для эксперимента создаётся отдельная БД/stream.
- Общая JSON Schema проверяет реальные входы/выходы HTTP и внешний payload;
  ошибки release_blocked имеют string[] details и отдельные blockers.
- UI после неизвестного исхода сохраняет команду при любом неуспешном повторе;
  HTTP запускается только после принятого reducer-перехода. Гонка poll/prepare
  даёт управляемый отказ отправки без исключения рендера.

Цена: новая редакция транспорта и регенерация обоих языков; старые клиенты
должны обновиться. Общая семантика подтверждения/устранения/выпуска DP-01 сохранена.
Пересмотр: после ответа №2 либо обнаружения контрпримера в A04/контрактных тестах.

Приёмка: отдельные реальные ERP HTTP и Uvicorn, PostgreSQL, импорт → confirm →
rework → новый цикл → контроль → resolve → released → ACK; проверка сохранённого
ERP snapshot/digest/решений, exact retry после lost ACK и неизменности старого B.

## Реализованный результат

- Контракт, generated, ERP, data и frontend перенесены из `e2f5e0e` (PR №6)
  squash-интеграцией без коммита. Исходники чужой ветки сохранены. Альтернативный
  coordinator и параллельная миграция №7 не включались: P4 остаётся единственным
  writer с `production_guard` и `quality_state_versions`.
- `contracts/openapi.yaml` и `external-openapi.yaml` — редакция **0.4.0** этой
  интеграции, Pydantic/TypeScript и shape baselines регенерированы. Новая редакция
  обязательных полей несовместима с 0.3.0; не объявляется совместимым patch.
- `app.main:app` теперь обслуживает общую схему; запросы и успешные ответы
  проходят JSON Schema. `app.legacy_main:app` явно сохраняет старый P3-показ.
- `SharedViews` строит текущие Item/Card/B под общим guard; поиск B идёт по
  revision и target, polling не плодит новые B. Старые B используют сохранённый
  контекст/анализ и точные raw IDs; при чтении provenance проверяется AEAD.
  Неизвестные factual composition/links версии остаются null.
- Resolve выполняет lookup raw → observations после durable dedupe внутри guard.
  В immutable Decision сохранены `control_record_ids` и `observation_ids`.
  Cycle проверяет последний rework disposition, фазу/run и новые сессии.
- Внешний `dp01-0.4` включается явно в закреплённом контексте нового stream.
  Пример профиля импорта повышен до версии 2. `QualityWorkflow._emit` формирует
  внешний snapshot перед digest; persistence проверяет внешнюю схему до commit.
  Worker передаёт сохранённое тело. Смена профиля существующего stream запрещена.
- UI сохраняет неизвестный исход при отказе повтору; допустимые гонки reducer
  возвращают управляемое состояние. Отправка стартует из эффекта после принятого
  перехода `sending`. Добавлены component tests с реальным React-рендером.
- Источники примера теперь передают initial control sessions; incoming-recheck
  без run. `emulator.replay rework_order` требует сохранённую серверную квитанцию
  `--cycle-receipt`, применяет её bindings и новые стабильные event IDs.

## Проверки и доказанный финал

Команды из `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not integration" -q
$env:QC_ROLE2_ROOT='D:\FINAL\final-hack'
.\.venv\Scripts\python.exe scripts/test_postgres.py -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pip check
```

Из корня, Python-окружение с `contracts/requirements.lock.txt`:

```text
python -m pytest contracts emulator -q
python -m contracts.generate --check
python -m contracts.compatibility contracts/openapi.yaml contracts/compat-baseline.json
python -m contracts.compatibility contracts/external-openapi.yaml contracts/external-compat-baseline.json
python -m emulator.generate
```

Из `frontend/`: `npm ci`, `npm test`, `npm run check:generated`, `npm run build`.

Результат: **160 backend без БД, 68 PostgreSQL, 27 contract/ERP, 11 frontend**.
Отдельно прошли генерация двух языков, обе shape-проверки, lint/format, build.
Проверки на Windows/Python 3.11/PostgreSQL 17.9. Добавлен CI job P5; удалённый
GitHub CI для текущих незакоммиченных изменений ещё не запускался.

`test_erp_import_a04.py[shared-ack]` проверяет:

1. Настоящее ERP HTTP → импорт полного заказа → Uvicorn/TCP → PostgreSQL.
2. Confirm → отдельный rework → новый серверный цикл с bindings → завершённая
   доработка → три новых контроля. Старое incoming не закрывает новый цикл.
3. Release до resolve запрещён. Resolve с raw другого типа, старого цикла или
   отсутствующим raw запрещён без эффектов. Успешное решение сохраняет raw и
   внутренние observation ссылки; exact retry после release возвращает его же.
4. После resolve: `confirmed_defects=1`, `resolved_confirmed_defects=1`,
   `open_confirmed_defects=0`, `rework_operations=1`, `released`, blockers пусты.
5. ERP коммитит финальное тело и намеренно теряет HTTP ACK. Durable worker
   повторяет исходное сообщение; получает duplicate с первоначальной квитанцией,
   проверяет stream/version/digest и сохраняет `acknowledged`.
6. После финального сообщения доставлены остальные версии этого stream;
   ERP остаётся на финальном released, `missing_version_ranges=[]`. Сверены
   точное тело, digest, confirm/resolution/disposition IDs и basis ID выпуска.
   Пересозданный SQLite Receiver читает тот же результат.

Дополнительно `test_shared_api.py`: normal release без дефектов имеет disposition
ID/B; late сохраняет эту ссылку и старое B; два признака одного raw имеют разные
индексы/области/карточки; смена контекста не меняет B1; release errors соответствуют
общей схеме. Компонент UI проверяет poll-before-submit (нет HTTP) и timeout →
retry 403 → retry success (все повторы побайтно сохраняют исходную команду).

## Запуск компонентов

1. PostgreSQL/API/worker — `deploy/compose.server.yaml`; каталог contracts
   монтируется read-only. Для Python API задаются обычные runtime переменные P4.
2. ERP из корня: `python -m emulator.server --port 8090`. Нужен
   `ERP_EMULATOR_TOKEN`, совпадающий с файлом токена worker/importer. Новый default
   SQLite — `emulator/state-v2-0.4.sqlite`; старый файл 0.3 не изменяется.
3. Импорт из backend: `python -m app.import_erp --url http://127.0.0.1:8090/external/v2/work-orders
   --contract ../contracts/external-openapi.yaml --profile examples/erp-import-profile.json
   --erp-token-file ../.local/erp.token --integration-token-file ../.local/import.token`.
4. `app.manage init` для **нового** каталога создаёт integration token и источники
   `vision-demo`/`line-demo`, соответствующие профилю. Существующая registry не
   перезаписывается; её владелец должен добавить нужные identities самостоятельно.
5. UI: `npm run dev` в frontend. `VISION_SOURCE_TOKEN`/`LINE_SOURCE_TOKEN` для
   replay соответствуют новым источникам. `python -m emulator.replay base_order`
   отправляет первоначальный цикл. Позднюю ветку не смешивать с A04 до отдельного
   разрешения противоречия.
6. Общий A04 без ручной настройки среды воспроизводится командой
   `python scripts/test_postgres.py tests/integration/test_erp_import_a04.py -q`
   из backend. Это автоматизированный серверный сценарий, не видеодемонстрация UI.

## Остаток и передача

Полный browser E2E со всем A04 и непрерывный скринкаст ещё не выполнены. Остальные
A01–A14/M0-END, scope/carry/disputed команды, межиздельный Evidence Graph и M0-R
не объявляются закрытыми этим сценарием. Новый B не выдумывает их версии.

№2: review редакции 0.4.0 и UI-исправлений; синхронизировать свою ветку с P4/P5,
расширять runner/UI на следующие сценарии. №1: сохранять единый guard/outbox,
закрывать оставшиеся сценарии по одному после совместного review. Выбранная
цель этой поставки — **один реальный путь до ACK ERP — проверена**.
