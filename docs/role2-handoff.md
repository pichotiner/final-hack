# Передача роли №2 на интеграцию

На 25 сентября 2026 роль №2 подготовила `contracts/openapi.yaml` по рабочему `docs/contracts.md` v0.2. Это **предложение транспортной формы для проверки ролью №1**, а не подтверждение, что backend уже ей соответствует. Производственные правила, серверные права, постоянный ID-маппинг, outbox и запуск принадлежат роли №1.

Нужно совместно решить перед подключением backend:

1. Подтвердить имена и обязательность полей чтения `Item`, `Nonconformity`, `TimelineEntry`, `Analytics`, `Delivery`. Markdown v0.2 описывает смысл, но не все точные DTO.
2. Определить выдачу токенов для UI/источников и правила CORS/прокси. Сейчас UI принимает вручную bearer token и не содержит секретов.
3. Подключить адаптер получения `GET /external/v1/work-orders` и результат через порт ядра. `emulator/server.py` готов как отдельный HTTP-процесс; `IdMap` — демонстрационное правило, не долговременное хранилище.
4. Дать backend-команды и доступ к PostgreSQL для сквозного теста. До этого UI проверен сборкой, эмулятор — HTTP-тестами, а полный путь пользователь → backend → БД → ACK не подтверждён.
5. Согласовать контрольный сценарий целостности. Роль №2 добавит его в демо, когда роль №1 задаст безопасную проверяемую операцию.

Сценарий `first-slice` дополняет таблицу `docs/first-slice.md` синтетическим `operator_action` в 10:08:30 после предупреждения станка. Действие является обстоятельством; оно не подтверждает причину и не меняет ожидаемые KPI.

Проверки генерации: `cd frontend && npm ci && npm run generate:types && npm run build`; для Python 3.11 установить `python -m pip install -r contracts/requirements.codegen.txt`, затем `datamodel-codegen --input contracts/openapi.yaml --input-file-type openapi --output contracts/generated/models.py --output-model-type pydantic_v2.BaseModel --target-python-version 3.11 --disable-timestamp`. CI проверяет diff, а `test:compat` показывает обнаружение двух намеренно несовместимых изменений. Проверка соответствия запущенного FastAPI контракту остаётся до появления API.
