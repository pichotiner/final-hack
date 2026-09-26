# P8: TLS между отдельными узлами

Четыре независимых Compose-проекта: PostgreSQL, ERP, ядро (API/importer/worker),
web. Общая Docker-сеть не требуется. Проверены четыре локально эмулированных
узла через опубликованные TCP-порты; физические разные машины не использовались.

| Канал между узлами | Защита |
| --- | --- |
| Браузер → web | HTTPS, leaf сертификат web |
| web → API proxy | HTTPS, `proxy_ssl_verify on`, CA и SAN имени API |
| importer/worker → ERP proxy | HTTPS, `QC_ERP_CA_FILE`, обязательный bearer |
| API/importer/worker/migrate → PostgreSQL | `sslmode=verify-full`, CA и SAN БД; `hostnossl ... reject` |

HTTP API и ERP опубликованы только во внутренней сети **своего узла**; наружу
выходит порт TLS proxy. Внутри одного узла proxy → приложение использует HTTP.
Это server-authenticated TLS плюс bearer/пароль БД, не mTLS.

## 1. Подготовка новой установки

Из корня репозитория, установленным backend Python:

```powershell
.\backend\.venv\Scripts\python.exe deploy/prepare_multihost.py --directory .local/hosts --api-host qc-api.example.internal --erp-host qc-erp.example.internal --postgres-host qc-db.example.internal --web-host qc-web.example.internal
```

Нужен новый каталог. Скрипт генерирует CA и отдельные ключи узлов, keyring,
учётные записи и per-node конфигурации. DNS-имена обязаны разрешаться с других
узлов. Порты по умолчанию — 8443 для HTTPS и 5432 для БД; поддерживаются
`--api-port`, `--erp-port`, `--postgres-port`, `--web-port`.

Каталог `issuer/` содержит CA private key и первоначальные credentials: он
остаётся у администратора выдачи, не монтируется в сервисы. Переносите на узел
только его каталог `api/`, `erp/`, `postgres/` или `web/`. Это bootstrap нового
пилота; существующую историю и ключи переносите через согласованное восстановление.

Корневой CA необходимо добавить в доверие тестового браузера. Сертификаты узлов
действуют 90 дней, CA — 365 дней. Для предприятия замените их сертификатами своей
PKI, сохраняя SAN, пути и права файлов. Обновление leaf: заменить crt/key и
перезапустить proxy; для PostgreSQL перезапустить его контейнер. Перед заменой CA
распространите trust bundle старого+нового CA клиентам, затем меняйте leaf.

## 2. Запуск на узлах

На каждой машине нужен checkout этой версии и Docker Compose. В закрытом контуре
образы и зависимости предварительно доставляются через локальный registry/cache.
`QC_NODE_DIR` — **абсолютный путь** перенесённого каталога конкретного узла.
`QC_BIND_ADDRESS` — адрес интерфейса этого узла, доступный остальным.
По умолчанию публикация ограничена loopback; для разнесения задайте частный IP.

Пример PowerShell для узла БД (адреса и пути заменить своими):

```powershell
$env:QC_NODE_DIR = "D:/efqa-secrets/postgres"
$env:QC_BIND_ADDRESS = "10.20.0.11"
docker compose --env-file "$env:QC_NODE_DIR/compose.env" -f deploy/multihost/database.yaml up -d --wait
```

Для Linux — эквивалентные `export QC_NODE_DIR=/srv/efqa-secrets/postgres` и
`export QC_BIND_ADDRESS=10.20.0.11`. Проверьте владельца файлов и `QC_UID/QC_GID`
в `compose.env`: API/ERP запускаются с этим UID, secret-файлы имеют режим 0600.

Порядок и команды на остальных узлах:

1. **ERP:** `QC_NODE_DIR` указывает на `erp/`;
   `docker compose --env-file "$env:QC_NODE_DIR/compose.env" -f deploy/multihost/erp.yaml up --build -d --wait`.
2. **Ядро:** `QC_NODE_DIR` указывает на `api/`;
   `docker compose --env-file "$env:QC_NODE_DIR/compose.env" -f deploy/multihost/core.yaml up --build -d --wait`.
   Сначала выполняются миграции, затем API, импорт ERP и worker.
3. **Web:** `QC_NODE_DIR` указывает на `web/`;
   `docker compose --env-file "$env:QC_NODE_DIR/compose.env" -f deploy/multihost/web.yaml up --build -d --wait`.

Вход: `https://qc-web.example.internal:8443`. Выдайте пользователям подходящие
токены из исходного `issuer/demo-tokens.json` защищённым каналом. На frontend-узел
эти токены копировать не требуется. Импорт создаёт задания, а не факты производства.

`runtime.env` ядра задаёт `QC_REQUIRE_TLS=1`: запуск с PostgreSQL без verify-full
и передача результатов ERP по HTTP отклоняются. Изменение DNS/портов после выдачи
требует обновления runtime.env, nginx.conf и при необходимости сертификата.
После смены адреса upstream перезапустите соответствующий proxy.

## 3. Проверки и восстановление

```powershell
# Проверка HTTPS/CA/SAN/запрета plaintext на локальной эмуляции узлов:
.\backend\.venv\Scripts\python.exe deploy/check_distributed_tls.py
# Четыре независимых проекта через published ports (Docker Desktop):
.\backend\.venv\Scripts\python.exe deploy/check_multihost.py
# Заполненная история, два ключа, backup/restore с внутренним TLS:
.\backend\.venv\Scripts\python.exe deploy/check_pilot.py --distributed-tls
```

Первый скрипт проверяет положительный TLS, неизвестный CA, неверное имя узла,
обязательный ERP bearer, TLS в `pg_stat_ssl`, запрет plaintext PostgreSQL.
Второй использует `host.docker.internal` только как адрес тестового host gateway;
на предприятии его заменяют реальные DNS-имена. Все контейнеры runner удаляет,
тома и локальные отчёты остаются в `.local`.

Для четырёх физических узлов backup требует согласованной паузы записи на всех:
остановить web/API proxy и импорт/worker, закрыть API и ERP; снять PostgreSQL dump,
SQLite ERP и все узловые credentials/keyring. Локальный `pilot.py backup` сам по
себе не управляет четырьмя удалёнными Docker daemon. Межузловое оркестрирование
резервирования, HA и RPO/RTO не входят в эту поставку.

## Короткий локальный запуск того же TLS-контура

```powershell
.\backend\.venv\Scripts\python.exe deploy/pilot.py up --directory .local/tls-pilot --port 8445 --seed --distributed-tls
```

Используйте новый каталог, чтобы получить отдельные сертификаты узлов. Маркер
TLS-профиля сохраняется, поэтому `down`, `verify`, backup и restore выбирают
нужные Compose-файлы автоматически. Требуется Compose с поддержкой `!override`
(2.24.4+). Обычный односерверный профиль без флага по-прежнему доступен.
