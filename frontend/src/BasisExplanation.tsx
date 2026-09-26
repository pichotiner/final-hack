import type { Basis } from "./api";
import { explainBasis, suggestedChecks } from "./basis-explanation";

export function BasisExplanation({ before, after }: { before: Basis; after: Basis }) {
  const explanation = explainBasis(before, after);
  return <aside aria-label="Что изменилось в основаниях">
    <h3>Что изменилось — коротко</h3>
    <p>Тогда: {before.basis_id} / revision {before.context_revision}. Сейчас: {after.basis_id} / revision {after.context_revision}.</p>
    <ol><li><strong>Свидетельства. </strong>{explanation.evidence}</li>
      <li><strong>Условия. </strong>{explanation.conditions}</li>
      <li><strong>Следующее действие. </strong>{explanation.action}</li></ol>
    <details><summary>Полные основания тогда и сейчас</summary><pre>{JSON.stringify({ then: before, now: after }, null, 2)}</pre></details>
  </aside>;
}

export function SuggestedChecks({ basis }: { basis: Basis }) {
  const checks = suggestedChecks(basis);
  return <aside aria-label="Следующий полезный контроль"><h3>Что нужно проверить</h3>
    <p>Подсказки по сохранённому B {basis.basis_id}, не назначение проверки и не разрешение выпуска.</p>
    {checks.length ? <ul>{checks.map(check => <li key={check.checkpoint}>
      <strong>{check.checkpoint}: {check.action}.</strong> {check.scope}. Метод/версия: {check.method}. {check.bindings}.
    </li>)}</ul> : <p>Неоценимых точек в этом B нет. Остальные ограничения и решения по изделию проверяются отдельно.</p>}
  </aside>;
}
