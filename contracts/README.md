# Контракты и автоматическое предотвращение рассинхронизации

Источники транспортных схем — `openapi.yaml` и `external-openapi.yaml`.
Python DTO генерируются через `python -m contracts.generate`, TypeScript —
через `npm run generate` из `frontend/`. Версии инструментов закреплены
в `requirements.lock.txt` и `frontend/package-lock.json`.

## Проверки перед поставкой

Из корня репозитория, в отдельном Python-окружении с установленным
`contracts/requirements.lock.txt`:

```powershell
python -m contracts.generate --check
python -m contracts.compatibility contracts/openapi.yaml contracts/compat-baseline.json
python -m contracts.compatibility contracts/external-openapi.yaml contracts/external-compat-baseline.json
python -m pytest contracts emulator -q
```

Из `frontend/`: `npm run check:generated`. Эти проверки включены в CI.
Shape-gate сигнализирует об изменении формы контракта; он не является полным
доказательством обратной совместимости произвольной JSON Schema.

## Короткая демонстрация обнаружения несовместимости

```powershell
python -m pytest contracts/test_compatibility.py -v
```

Тесты меняют копию контракта в памяти: тип `DefectSign.description`, enum,
обязательность, nullable, элементы массива и схему ответа. Проверка обязана
обнаружить изменения. Поля с именами `description`, `title`, `summary` и другими
документационными словами проверяются как обычные поля данных. Изменение текста
аннотации не меняет shape; значения `const`, `enum` и `default` сохраняются целиком.
Исходные спецификации при демонстрации не изменяются.

Baseline обновляется через `contracts.compatibility ... --write` только после
проверки изменений схемы. В текущем исправлении gate baseline заново включает
два ранее пропущенных строковых поля `description`; транспортный контракт не менялся.

Проверка отказа некорректного источника, из `backend/`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_source_boundary.py -v
```

Запросы к настоящему `/api/v2/events` через TestClient с `state=[]`, `{}`,
`null`, числом, boolean и неизвестной строкой получают структурированный 422
с `request_id`. Заглушка запрещает обращение к ingestion: неверный вход
отсекается до записи. Сетевой транспорт и PostgreSQL этим узким тестом не проверяются.
