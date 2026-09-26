# Пилот P7: запуск и восстановление

Профиль `compose.pilot.yaml` запускает PostgreSQL, миграции, API, однократный
импорт ERP, worker, SQLite ERP-эмулятор и HTTPS frontend. Требуются Docker Compose
и backend virtualenv с `backend/requirements.lock.txt`. Все команды ниже — из
корня репозитория в PowerShell.

## Запуск и доступ

```powershell
.\backend\.venv\Scripts\python.exe deploy/pilot.py up --directory .local/pilot --port 8443 --seed
```

Адрес `https://localhost:8443`. В `.local/pilot/credentials/` создаются локальные
синтетические токены, registry хешей, пароли БД, keyring и TLS CA/сертификат.
Существующая установка не сбрасывается. Каталог игнорируется git. Для браузера
доверяйте `tls/ca.crt` в тестовом профиле; скрипт и `curl --cacert` в healthcheck
проверяют этот CA без отключения проверки сертификата.

Из `demo-tokens.json` используйте `controller` для решений ОТК, `master` для
нового цикла, `integration` для чтения доставок. Это токены демонстратора, UI
держит их только в памяти вкладки. Фото не эмулируются как реальные доказательства.

Без `--seed` импортируются только задания; с ним принимается синтетический набор
источников. `--hostname` при создании добавляет имя/IP в сертификат. Публикация
по умолчанию — loopback, изменение адреса требует `QC_BIND_ADDRESS` в окружении.
Не меняйте порт в командах запуска/restore без учёта занятого порта.

```powershell
.\backend\.venv\Scripts\python.exe deploy/pilot.py verify --directory .local/pilot
.\backend\.venv\Scripts\python.exe deploy/pilot.py down --directory .local/pilot
```

`down` сохраняет том PostgreSQL, SQLite и credentials. Идентичность Compose-проекта
вычисляется из абсолютного пути установки: повторный запуск использует тот же
каталог. Для переноса на другую машину используйте restore.

## Ключи и учётные данные

```powershell
.\backend\.venv\Scripts\python.exe deploy/pilot.py rotate-key --directory .local/pilot
.\backend\.venv\Scripts\python.exe deploy/pilot.py rotate-identity --directory .local/pilot --actor controller
```

Keyring содержит `active` и список файлов всех доступных версий. Ротация создаёт
новый ключ и атомарно заменяет manifest, затем перезапускает API/worker. Старые
ключи нужны для истории и backup, удалять их нельзя до окончания их срока хранения.
`QC_KEYRING_FILE` — конфигурация нового runtime; одиночные `QC_EVENT_KEY_*`
сохранены для прежних окружений.

Ротация личности отзывает старый bearer, печатает **путь** нового credential-файла.
`demo-tokens.json` остаётся первоначальной выдачей: актуальный токен берите из
нового файла. Если исход команды неизвестен, в UI используйте «Восстановление
доступа», а затем точный повтор. Другой actor не принимается как замена автора.

## Резервная копия

Перед командой задайте `QC_BACKUP_PASSPHRASE` из локального защищённого источника.
Она не сохраняется в backup и потребуется на другой машине.

```powershell
.\backend\.venv\Scripts\python.exe deploy/pilot.py backup --directory .local/pilot --output .local/backup-001
```

Выходной каталог должен быть новым. Скрипт останавливает worker/importer и web,
проверяет свидетельства, останавливает API/ERP и сохраняет:

- `postgres.dump` — PostgreSQL custom-format dump;
- `erp.sqlite` — закрытую SQLite ERP после остановки процесса;
- `secrets.aesgcm` — credentials, TLS и keyring в AES-GCM архиве с ключом из Scrypt;
- `manifest.json` — хеши файлов и fingerprints свидетельств.

В `finally` сервисы запускаются снова. Не используйте незавершённый backup без
manifest. PostgreSQL/ERP dump содержат открытые проекции; храните весь каталог
в защищённом хранилище, manifest закрепляйте отдельно. Хеш рядом с файлом не
является независимым доказательством отсутствия подмены.

## Изолированное восстановление

```powershell
.\backend\.venv\Scripts\python.exe deploy/pilot.py restore --directory .local/restored-001 --backup .local/backup-001 --port 8444
```

Нужен **новый** каталог и свободный порт. Создаются отдельный Compose-проект и
том. Проверяются хеши backup, расшифровывается keyring, восстанавливаются PostgreSQL
и ERP, применяются миграции/права. Верификатор проверяет raw AEAD, основания B,
состояние и outbox; fingerprints должны совпасть с manifest. Все сообщения
повторно доставляются ERP, ACK проверяется, неизменяемая история сверяется ещё раз.
Результат — `restore-report.json`, после чего доступен HTTPS на выбранном порту.

На другой Linux-машине проверьте UID/GID в `credentials/compose.env` и владельца
восстановленных secret/ERP файлов: контейнеры работают под указанным пользователем.
Сборка использует зависимости/образы; для закрытого контура заранее подготовьте
локальный registry/cache. Межхостовый TLS, HA и достижение RPO/RTO этим профилем
не реализуются: внутренний HTTP находится в private-сети одного Docker-host.

## Проверка регламента одной командой

```powershell
.\backend\.venv\Scripts\python.exe deploy/check_pilot.py
```

Скрипт создаёт изолированные source/restore установки, принимает события,
подтверждает дефект, меняет ключ v1 → v2 с restart, читает старое B, принимает
новый raw, делает backup/restore и сверяет историю после повторной доставки ERP.
После проверки контейнеры останавливаются и удаляются, тома и отчёты сохраняются.
Backup smoke-теста использует одноразовый пароль в памяти; для постоянной копии
применяйте обычную команду `backup` со своим сохранённым паролем.
