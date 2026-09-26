# P1: acceptance slice for quality and release

Статус: **реализован в доменном reference-срезе; PostgreSQL boundary обновлён, полный повторный integration-run ожидает доступный Docker Engine.**

## Приёмочные свойства

Реализация P1 не смешивает четыре разные вещи:

1. наблюдение и его оценимость;
2. существование несоответствия;
3. фактическую доработку;
4. решение о выпуске изделия.

### Блокировка выпуска

`release_allowed_now=false`, если есть хотя бы одно из условий:

- неизвестен маршрут;
- отсутствует обязательный checkpoint текущего цикла;
- checkpoint имеет `not_assessable`;
- есть противоречивые активные наблюдения;
- карточка `pending_review` или `needs_check`;
- подтверждённый дефект имеет `resolution_status=open`;
- есть `review_required` после позднего основания;
- новый цикл доработки не имеет завершённой rework-операции.

Плохое наблюдение не может быть исправлено простой кнопкой. Новый контроль
должен явно содержать `replaces_observation_id` и ссылаться на тот же checkpoint
и цикл. Старое наблюдение остаётся в истории.

### Доработка и повторный контроль

`disposition=rework` только назначает доработку. Выполнением считается отдельная
зарегистрированная `ReworkOperation` со статусом `completed`, активным циклом и
`previous_operation_run_id`. Только такая операция может участвовать в `resolve`.

Принятый путь:

```text
confirm
→ disposition=rework
→ новый control cycle
→ completed rework operation
→ repeat control
→ resolve
→ отдельный disposition=released
```

Подтверждённый дефект не исчезает после `resolve`:

```text
confirmed_defects = 1
open_confirmed_defects = 0
resolved_confirmed_defects = 1
rework_operations = 1
```

После выпуска позднее свидетельство сохраняет прежний `released` в истории,
но немедленно переводит текущий результат в `release_allowed_now=false` и
создаёт blocker для повторного рассмотрения.

## KPI

`QualityWorkflow.analytics()` принимает явную когорту с удалением дублей ID и
возвращает:

- `inspected_items`;
- `confirmed_nonconforming_items`;
- `confirmed_defects`;
- `open_confirmed_defects`;
- `resolved_confirmed_defects`;
- `open_nonconforming_items`;
- `review_required_items`;
- `rework_operations`;
- `repeat_controls`;
- `blocked_items`;
- `released_after_reinspection`.

Повтор транспортного события не меняет ревизию, KPI или outbox. Счётчик
`rework_operations` растёт только при новой завершённой операции, а не при
`disposition` или `resolve`.

## Проверка

`backend/tests/test_quality_workflow.py` содержит сценарии:

- полный путь `confirm → rework → cycle → operation → repeat control → resolve → release`;
- late evidence после уже сохранённого release;
- `poor/unknown + no_defect_signs` и явная замена `not_assessable`;
- два изделия в одной явной когорте без двойного подсчёта;
- повтор команды, повтор события и ERP V2→V1.

PostgreSQL-проверка также сохраняет basis, decisions, operation-related state и
outbox в одной транзакционной проекции под advisory lock изделия.
