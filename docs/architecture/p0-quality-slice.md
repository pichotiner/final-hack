# P0: executable quality-decision slice

Статус: **реализован reference-срез роли №1; PostgreSQL-адаптер и миграция добавлены, HTTP/API и UI ещё подключаются ролью №2.**

## Что закрыто

`backend/app/domain/quality.py` реализует семантику DP-01:

- immutable `Basis` с `basis_id` и `context_revision`;
- stale command rejection (`stale_revision`) и mismatch проверки основания;
- отдельные `confirm/reject/request_check` для карточки несоответствия;
- отдельное `resolve` после завершённой доработки и нового хорошего контроля;
- отдельный disposition `rework/released/rejected`;
- полнота текущего цикла и blockers для выпуска;
- KPI, где устранённый подтверждённый дефект остаётся в `confirmed_defects`;
- idempotency command key до проверки текущей ревизии;
- immutable full ERP snapshot в outbox с монотонным `result_version`;
- digest-checked ACK и reference receiver для `applied/superseded/duplicate`.

`backend/app/adapters/quality.py` сохраняет этот aggregate в PostgreSQL под
advisory lock изделия. Миграция `0003_quality_workflow` добавляет таблицы
`quality_items`, `quality_bases`, `quality_commands`, `quality_decisions` и
`quality_outbox`. Решения и основания не обновляются: текущая проекция хранит
сводное состояние, а история команд/outbox сохраняется отдельными строками.

## Минимальный сценарий M0

Тест `backend/tests/test_quality_workflow.py` проверяет:

1. позднее событие после открытия B отклоняет старую команду;
2. принятое подтверждение сохраняется после позднего свидетельства;
3. повтор команды не создаёт новую запись или outbox/KPI эффект;
4. выпуск блокируется до полного контроля и устранения;
5. `confirm → rework → новый cycle → repeat control → resolve → released`;
6. полный snapshot принимается ERP-эмулятором, а повтор ACK безопасен.

Транспортный слой должен передавать в workflow серверные `actor_id`,
`expected_revision`, `expected_basis_id` и `idempotency_key`; клиент не может
сам сформировать B из списка «последних» событий.

## Граница реализации

Это не объявляет промышленную готовность. Реализация не включает HTTP,
RBAC-политику, UI закреплённого рассмотрения, импорт задания или реальную ERP.
Она даёт проверяемое ядро и PostgreSQL boundary, к которому роль №2 подключает
OpenAPI v0.3.0, UI и эмулятор по DP-01.
