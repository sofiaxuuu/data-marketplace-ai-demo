"use client";

import { useEffect, useState } from "react";
import type { Workflow } from "./use-workflow";

export default function SqlWorkflow({ run, busy, step, onAction }: { run: Workflow; busy: boolean; step: 2 | 3 | 4; onAction: (action: object) => void }) {
  const [confirmed, setConfirmed] = useState(run.confirmed);
  const [approved, setApproved] = useState(run.approved);
  const product = run.selected!;
  const external = !!run.external_file;
  const confirmAction = external ? "confirm_external" : "confirm_source";
  const { plan, result } = run;
  useEffect(() => { setConfirmed(run.confirmed); setApproved(run.approved); }, [run.id, product.id, product.version, run.plan?.run_id, run.confirmed, run.approved]);

  return <section className="sql-workflow" aria-label="Generate and execute SQL">
    {step === 2 && <>
    <label><input type="checkbox" checked={confirmed} disabled={busy || !run.allowed_actions.includes(confirmAction)} onChange={e => setConfirmed(e.target.checked)} /> I confirm {product.name} (version {product.version}) fits this question.</label>
    <p className="catalog-note">Generating sends your question and this product’s metadata/schema to OpenAI—not sample rows. Execution stays local.</p>
    <button className="primary" disabled={busy || !confirmed || !run.allowed_actions.includes(confirmAction)} onClick={() => onAction({ type: confirmAction, confirmed: true })}>Generate SQL for review</button>
    </>}
    {step === 3 && <><div className="section-heading"><span className="step-number" aria-hidden="true">03</span><h2>Review SQL</h2></div>
    {plan && <div aria-live="polite">
      <h4>{plan.outcome === "ready" ? "Review the query plan" : plan.outcome === "clarify" ? "Please clarify your question" : "This dataset cannot answer that question"}</h4>
      <p>{plan.reason}</p>
      {plan.outcome === "clarify" && <p>{run.conversation_id ? "Ask a clearer question in the composer below." : "Update the question above, then choose and confirm a source again."}</p>}
      {plan.outcome === "abstain" && <><p>A different dataset may help with missing data, but not an unsupported SQL operation.</p>
        {run.conversation_id ? <p>Choose another dataset in the sidebar to start a new conversation, or ask a different question about this dataset.</p>
          : <button disabled={busy || !run.allowed_actions.includes("recover_local")} onClick={() => onAction({ type: "recover_local" })}>Suggest another local dataset</button>}</>}
      {plan.outcome === "ready" && <>
        <p><strong>Input fields:</strong> {plan.selected_fields.join(", ")}</p>
        {!!plan.formulas.length && <><h4>Formulas</h4><ul>{plan.formulas.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
        {!!plan.assumptions.length && <><h4>Interpretation / assumptions</h4><ul>{plan.assumptions.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
        <p><strong>Output units:</strong> {plan.result_units.join(" · ")}</p>
        <pre className="review-sql"><code>{plan.sql}</code></pre>
        <p>Model: {plan.model} · Planning: {plan.planning_ms?.toFixed(0)} ms · Up to {plan.row_limit} result rows · Plans expire after one hour.</p>
        <p>Safety and schema checks passed. This does not prove the analysis is correct: verify periods, units and formulas.</p>
        <label><input type="checkbox" checked={approved} disabled={busy || !run.allowed_actions.includes("approve_sql")} onChange={e => setApproved(e.target.checked)} /> I approve this SQL and its interpretation.</label>
        <div><button className="primary" disabled={busy || !approved || !run.allowed_actions.includes("approve_sql")} onClick={() => onAction({ type: "approve_sql", approved: true, plan_id: plan.run_id })}>Execute approved SQL</button></div>
      </>}
    </div>}</>}
    {step === 4 && result && <section aria-live="polite">
      <div className="section-heading"><span className="step-number" aria-hidden="true">04</span><h2>Results</h2></div><p>{result.answer}</p>
      {result.outcome === "no_data" && <p className="notice">No rows matched the approved query. Check its filters and the snapshot coverage.</p>}
      {!!result.rows.length && <div className="result-table-scroll"><table><thead><tr>{result.columns.map(c => <th scope="col" key={c}>{c}</th>)}</tr></thead><tbody>{result.rows.map((row, i) => <tr key={i}>{result.columns.map(c => <td key={c}>{row[c] === null ? "NULL" : String(row[c])}</td>)}</tr>)}</tbody></table></div>}
      {result.truncated && <p className="notice">Results truncated. Narrow the question to see a smaller slice.</p>}
      <p>Source: {run.external_file?.origin === "upload" ? `Uploaded ${run.external_file.filename ?? "CSV"}` : <a href={product.source_url} target="_blank" rel="noreferrer">{product.source_name}</a>} · Snapshot {product.snapshot_date} · {external ? "Temporary CSV" : `Product v${product.version}`} · Execution {result.execution_ms.toFixed(0)} ms</p>
      <details><summary>Run trace</summary><ol>{result.trace.map((step, i) => <li key={i}>{step.stage}: {step.result}</li>)}</ol></details>
    </section>}
  </section>;
}
