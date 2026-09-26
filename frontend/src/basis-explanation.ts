import type { Basis } from "./api";
import { basisDiff } from "./review-state";

const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return JSON.stringify(value.map(canonical).sort());
  if (value !== null && typeof value === "object") return JSON.stringify(
    Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, part]) => [key, canonical(part)]));
  return JSON.stringify(value);
};

/** Read-only explanation of saved server snapshots, never a release decision. */
export function explainBasis(before: Basis, after: Basis) {
  if (before.item_id !== after.item_id || before.target_id !== after.target_id) {
    return { comparable: false, evidence: "Выбраны разные изделия или цели рассмотрения.",
      conditions: "Сравнение оснований недопустимо.", action: "Откройте два B одного изделия и одной цели." };
  }
  if (before.context_revision > after.context_revision) {
    return { comparable: false, evidence: "Основание «сейчас» старше основания «тогда».",
      conditions: "Направление сравнения перепутано.", action: "Поменяйте основания местами или загрузите актуальное B." };
  }
  const diff = basisDiff(before, after);
  const changed = [
    before.control_cycle_id !== after.control_cycle_id ? "цикл контроля" : null,
    diff.changedVersions.length ? `версии: ${diff.changedVersions.join(", ")}` : null,
    canonical(before.completeness) !== canonical(after.completeness) ? "полнота обязательного контроля" : null,
    canonical(before.blockers) !== canonical(after.blockers) ? "ограничения выпуска" : null,
    canonical(before.unknowns) !== canonical(after.unknowns) ? "неизвестные сведения" : null,
    canonical(before.contradictions) !== canonical(after.contradictions) ? "противоречия" : null,
    canonical(before.analysis_snapshot) !== canonical(after.analysis_snapshot) ? "сохранённый анализ" : null,
    canonical(before.decision_ids) !== canonical(after.decision_ids) ? "решения" : null,
  ].filter(Boolean);
  const inputsChanged = diff.added.length + diff.removed.length + diff.changedRoles.length > 0;
  return {
    comparable: true,
    evidence: `Новых исходников: ${diff.added.length}; исключённых из набора: ${diff.removed.length}; изменённых ролей/причин: ${diff.changedRoles.length}.` +
      (diff.added.length ? ` Новые record ID: ${diff.added.map(x => x.record_id).join(", ")}.` : ""),
    conditions: changed.length ? `Изменились: ${changed.join("; ")}.` : "В сравниваемых условиях изменений не найдено.",
    action: inputsChanged || changed.length
      ? "Откройте новое рассмотрение, изучите изменения и примите отдельное решение. Старое решение сохраняется; причина дефекта автоматически не устанавливается."
      : "Новых различий для объяснения нет. Возможность выпуска проверяется сервером при отдельном решении.",
  };
}

export function suggestedChecks(basis: Basis) {
  return basis.completeness.filter(point => point.assessment_status !== "assessable").map(point => ({
    checkpoint: point.checkpoint_id,
    scope: `Компонент: ${point.component_id ?? "неизвестен"}; область: ${point.region_id ?? "неизвестна"}`,
    method: point.method_version ?? "Метод не определён — уточнить у технолога",
    action: point.assessment_status === "missing" ? "Выполнить отсутствующий обязательный контроль"
      : point.assessment_status === "disputed" ? "Разобрать противоречащие результаты с ОТК; новый сигнал сам по себе не снимает противоречие"
        : "Восстановить качество наблюдения и назначить оценимый контроль",
    bindings: `Цикл ${point.control_cycle_id}; сессия ${point.control_session_id ?? "не определена"}; операция ${point.operation_run_id ?? "не задана"}`,
  }));
}
