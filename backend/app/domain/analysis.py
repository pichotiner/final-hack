"""Explainable comparisons and auditable aggregates; no inferred operator blame."""

from datetime import datetime


def circumstances(item, observations, usable, candidate_key):
    result = []
    timeline = item.production_history.get("timeline", [])
    for card in item.nonconformities.values():
        signs = sorted(
            (
                o
                for o in observations
                if candidate_key(o) == card.candidate_key
                and o.effective_result == "defect_signs"
                and usable(o)
            ),
            key=lambda o: (o.occurred_at, o.observation_id),
        )
        sign = signs[0] if signs else None
        before = (
            []
            if sign is None
            else [
                o
                for o in observations
                if usable(o)
                and o.effective_result == "no_defect_signs"
                and o.occurred_at < sign.occurred_at
                and (o.cycle_id, o.component_id, o.region_id, o.source_id, o.analyzer_version)
                == (
                    sign.cycle_id,
                    sign.component_id,
                    sign.region_id,
                    sign.source_id,
                    sign.analyzer_version,
                )
            ]
        )
        previous = max(before, key=lambda o: o.occurred_at) if before else None
        operations = [
            op
            for op in item.production_history.get("operations", [])
            if sign
            and op.get("status") == "completed"
            and "uncertain_source_time" not in op.get("issues", [])
            and op.get("finished_at")
            and datetime.fromisoformat(op["finished_at"]) <= sign.occurred_at
            and (
                previous is None
                or datetime.fromisoformat(op["finished_at"]) >= previous.occurred_at
            )
        ]
        runs = {op["operation_run_id"] for op in operations}
        related = [
            e
            for e in timeline
            if e.get("operation_run_id") in runs
            and e["event_type"] in {"machine_state", "operator_action"}
            and sign
            and e.get("time_quality") == "synchronized"
            and datetime.fromisoformat(e["occurred_at"]) <= sign.occurred_at
            and (
                previous is None or datetime.fromisoformat(e["occurred_at"]) >= previous.occurred_at
            )
        ]
        alternatives = []
        if sign and sign.inspection_phase == "incoming":
            alternatives.append(
                {
                    "category": "incoming",
                    "explanation": "Признак уже виден на входном контроле.",
                    "record_ids": [sign.record_id],
                }
            )
        if previous:
            alternatives.append(
                {
                    "category": "process",
                    "explanation": (
                        "Признак появился между сопоставимыми осмотрами; "
                        "конкретная причина не установлена."
                    ),
                    "record_ids": [previous.record_id, sign.record_id],
                }
            )
        warnings = [
            e["record_id"]
            for e in related
            if e["event_type"] == "machine_state"
            and (e.get("details") or {}).get("state") in {"warning", "stopped"}
        ]
        if warnings:
            alternatives.append(
                {
                    "category": "equipment",
                    "explanation": (
                        "В интервале зарегистрировано отклонение оборудования; "
                        "причинная связь требует проверки."
                    ),
                    "record_ids": warnings,
                }
            )
        alternatives.append(
            {
                "category": "unknown",
                "explanation": (
                    "Входной дефект, особенности метода или незарегистрированное "
                    "воздействие остаются альтернативами."
                ),
                "record_ids": [],
            }
        )
        contrary = [
            o.record_id
            for o in observations
            if sign
            and usable(o)
            and o.effective_result == "no_defect_signs"
            and o.occurred_at >= sign.occurred_at
            and (o.cycle_id, o.component_id, o.region_id, o.source_id, o.analyzer_version)
            == (
                sign.cycle_id,
                sign.component_id,
                sign.region_id,
                sign.source_id,
                sign.analyzer_version,
            )
        ]
        result.append(
            {
                "nonconformity_id": card.nonconformity_id,
                "status": "hypothesis",
                "before_record_id": previous.record_id if previous else None,
                "after_record_id": sign.record_id if sign else None,
                "operation_run_ids": sorted(runs),
                "related_events": related,
                "alternatives": alternatives,
                "comparison": {
                    "component_id": sign.component_id if sign else None,
                    "region_id": sign.region_id if sign else None,
                    "method_version": sign.analyzer_version if sign else None,
                },
                "contradicting_record_ids": sorted(set(contrary)),
                "limitations": ["Порядок событий и действия оператора не доказывают вину."]
                + (
                    [
                        "Есть последующие отрицательные осмотры того же цикла; "
                        "требуется разбор противоречия."
                    ]
                    if contrary
                    else []
                )
                + (
                    []
                    if previous
                    else ["Нет пригодного сопоставимого отрицательного осмотра до признака."]
                )
                + ([] if sign else ["Нет пригодного положительного осмотра."]),
            }
        )
    return result


def aggregate(items, *, since=None, until=None):
    """Period is [since, until); current card status, event-time attribution."""

    def within(value):
        if value is None:
            return since is None and until is None
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        return (since is None or moment >= since) and (until is None or moment < until)

    defects, durations, work = {}, {}, {}
    inspected = set()
    for item in items:
        from app.domain.quality import QualityWorkflow

        observations = QualityWorkflow._effective_observations(item)
        entries = {e["record_id"]: e for e in item.production_history.get("timeline", [])}
        if any(QualityWorkflow._usable(item, o) and within(o.occurred_at) for o in observations):
            inspected.add(item.item_id)
        for nc in item.nonconformities.values():
            if nc.status != "confirmed":
                continue
            signs = sorted(
                (o for o in observations if QualityWorkflow._candidate_key(o) == nc.candidate_key),
                key=lambda o: (o.occurred_at or o.created_at, o.observation_id),
            )
            if not signs or not within(signs[0].occurred_at):
                continue
            obs = signs[0]
            entry = entries.get(obs.record_id, {})
            key = (
                obs.defect_type,
                entry.get("line_id"),
                entry.get("station_id"),
                nc.cause_status,
                nc.cause_category,
            )
            group = defects.setdefault(
                key,
                {
                    "defect_type": key[0],
                    "line_id": key[1],
                    "station_id": key[2],
                    "cause_status": key[3],
                    "cause_category": key[4],
                    "nonconformity_ids": [],
                    "item_ids": [],
                    "record_ids": [],
                    "decision_ids": [],
                },
            )
            group["nonconformity_ids"].append(nc.nonconformity_id)
            group["item_ids"].append(item.item_id)
            group["record_ids"].extend(o.record_id for o in signs if o.record_id)
            group["decision_ids"].append(nc.decision_ids[-1])
        for op in item.production_history.get("operations", []):
            if not within(op.get("finished_at") or op.get("started_at")):
                continue
            dimensions = {
                "item_id": item.item_id,
                **{
                    k: op.get(k)
                    for k in (
                        "line_id",
                        "station_id",
                        "operator_id",
                        "shift_id",
                        "operation_type_id",
                    )
                },
            }
            for duration in op.get("durations", []):
                key = (*dimensions.values(), duration["kind"], duration["origin"])
                group = durations.setdefault(
                    key,
                    {
                        **dimensions,
                        "kind": duration["kind"],
                        "origin": duration["origin"],
                        "unit": "s",
                        "total_seconds": 0,
                        "measured_operations": 0,
                        "unavailable_operations": 0,
                        "completed_operations": 0,
                        "incomplete_operations": 0,
                        "invalid_operations": 0,
                        "operation_run_ids": [],
                        "record_ids": [],
                    },
                )
                group[
                    "completed_operations"
                    if op["status"] == "completed"
                    else "invalid_operations"
                    if op["status"] == "invalid"
                    else "incomplete_operations"
                ] += 1
                if duration["value"] is None or op["status"] != "completed":
                    group["unavailable_operations"] += 1
                else:
                    group["total_seconds"] += duration["value"]
                    group["measured_operations"] += 1
                group["operation_run_ids"].append(op["operation_run_id"])
                group["record_ids"].extend(duration.get("evidence_record_ids", []))
            if op["status"] != "completed":
                continue
            comparison = {
                "item_type_id": item.production_history.get("item_type_id"),
                "route_version": item.context.route_version if item.context else None,
                **{
                    k: op.get(k)
                    for k in ("operation_type_id", "station_id", "operator_id", "shift_id")
                },
            }
            key = tuple(comparison.values())
            group = work.setdefault(
                key,
                {
                    **comparison,
                    "completed_operations": 0,
                    "confirmed_errors": 0,
                    "item_ids": [],
                    "operation_run_ids": [],
                    "record_ids": [],
                    "decision_ids": [],
                },
            )
            group["completed_operations"] += 1
            group["item_ids"].append(item.item_id)
            group["operation_run_ids"].append(op["operation_run_id"])
            group["record_ids"].extend(op.get("evidence_record_ids", []))
            latest = {
                nc.decision_ids[-1]
                for nc in item.nonconformities.values()
                if nc.status == "confirmed"
                and nc.cause_status == "established"
                and nc.cause_category == "operator"
                and nc.decision_ids
            }
            decisions = [
                d
                for d in item.decisions
                if d.decision_id in latest
                and d.operation_run_id == op["operation_run_id"]
                and d.operator_id == op.get("operator_id")
            ]
            # Count erroneous operations, not multiple defect cards from one action.
            group["confirmed_errors"] += int(bool(decisions))
            group["decision_ids"].extend(d.decision_id for d in decisions)
    for group in work.values():
        group["comparable"] = all(
            group[key] is not None
            for key in ("item_type_id", "route_version", "operation_type_id", "operator_id")
        )
        group["error_rate"] = (
            group["confirmed_errors"] / group["completed_operations"]
            if group["comparable"]
            else None
        )
    for group in defects.values():
        group["confirmed_defects"] = len(group["nonconformity_ids"])
    for group in [*defects.values(), *durations.values(), *work.values()]:
        for key, value in group.items():
            if isinstance(value, list):
                group[key] = sorted(set(value))
    return {
        "period": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "period_inspected_items": len(inspected),
        "defects": list(defects.values()),
        "durations": list(durations.values()),
        "comparable_work": list(work.values()),
        "definitions": [
            "counts — текущее состояние всей выбранной когорты, без ограничения периода.",
            "Период [since, until): первый осмотр дефекта / завершение операции "
            "(для незавершённых — начало).",
            "Линия и участок дефекта — место первого обнаружения, "
            "не установленное место возникновения.",
            "Сравнимость работ: тип изделия, тип операции и закреплённая версия маршрута; "
            "при неизвестных атрибутах доля не выводится.",
            "Ошибки — операции с установленной человеком причиной operator; "
            "знаменатель — сопоставимые завершённые операции.",
            "Разные kind/origin длительности не суммируются между собой; "
            "null — неизвестный атрибут.",
        ],
    }
