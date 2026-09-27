"use client";

import { useEffect, useRef, useState } from "react";
import type { InspectProduct } from "./candidate-inspector";

type Plan = {
  outcome: "ready" | "clarify" | "abstain"; reason: string; sql: string;
  selected_fields: string[]; formulas: string[]; assumptions: string[]; result_units: string[];
  run_id?: string; model: string; planning_ms?: number; row_limit?: number;
  trace?: { stage: string; result: string }[];
};
type Result = { outcome: "answered" | "no_data"; answer: string; columns: string[];
  rows: Record<string, string | number | boolean | null>[]; truncated: boolean;
  execution_ms: number; trace: { stage: string; result: string }[] };

export default function SqlWorkflow({ product, question }: { product: InspectProduct; question: string }) {
  const [confirmed, setConfirmed] = useState(false);
  const [approved, setApproved] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => {
    generation.current += 1;
    setConfirmed(false); setApproved(false); setPlan(null); setResult(null); setBusy(false); setError("");
    return () => { generation.current += 1; };
  }, [question, product.id, product.version]);

  async function request(action: "generate" | "execute") {
    const current = ++generation.current;
    setBusy(true); setError(""); setResult(null);
    if (action === "generate") { setPlan(null); setApproved(false); }
    try {
      const response = await fetch(`/api/sql-runs/${action}`, {
        method: "POST", headers: { "content-type": "application/json" },
        signal: AbortSignal.timeout(75000),
        body: JSON.stringify(action === "generate" ? {
          question: question.trim(), product_id: product.id, manifest_version: product.version, confirmed: true,
        } : { run_id: plan?.run_id, approved: true }),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "Request failed. Check the question and try again.");
      if (generation.current === current) {
        if (action === "generate") setPlan(value);
        else setResult(value);
      }
    } catch (e) {
      if (generation.current === current) setError(e instanceof Error ? e.message : "SQL workflow unavailable");
    } finally { if (generation.current === current) setBusy(false); }
  }

  return <section className="sql-workflow" aria-label="Generate and execute SQL">
    <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => {
      generation.current += 1; setConfirmed(e.target.checked); setPlan(null); setResult(null); setApproved(false);
    }} /> I confirm {product.name} (version {product.version}) fits this question.</label>
    <p className="catalog-note">Generating sends your question and this product’s metadata/schema to OpenAI—not sample rows. Execution stays local.</p>
    <div className="section-heading"><span className="step-number" aria-hidden="true">03</span><h2>Generate and review SQL</h2></div>
    {!question.trim() && <p>Enter a business question above before generating SQL.</p>}
    <button className="primary" disabled={busy || !confirmed || !question.trim()} onClick={() => void request("generate")}>{busy && !plan ? "Planning SQL…" : "Generate SQL for review"}</button>
    {error && <p role="alert" className="notice error">{error}</p>}
    {plan && <div aria-live="polite">
      <h4>{plan.outcome === "ready" ? "Review the query plan" : plan.outcome === "clarify" ? "Please clarify your question" : "This dataset cannot answer that question"}</h4>
      <p>{plan.reason}</p>
      {plan.outcome === "ready" && <>
        <p><strong>Input fields:</strong> {plan.selected_fields.join(", ")}</p>
        {!!plan.formulas.length && <><h4>Formulas</h4><ul>{plan.formulas.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
        {!!plan.assumptions.length && <><h4>Interpretation / assumptions</h4><ul>{plan.assumptions.map((s, i) => <li key={i}>{s}</li>)}</ul></>}
        <p><strong>Output units:</strong> {plan.result_units.join(" · ")}</p>
        <pre className="review-sql"><code>{plan.sql}</code></pre>
        <p>Model: {plan.model} · Planning: {plan.planning_ms?.toFixed(0)} ms · Up to {plan.row_limit} result rows · Plans expire after one hour.</p>
        <p>Safety and schema checks passed. This does not prove the analysis is correct: verify periods, units and formulas.</p>
        <label><input type="checkbox" checked={approved} disabled={busy} onChange={e => setApproved(e.target.checked)} /> I approve this SQL and its interpretation.</label>
        <div><button className="primary" disabled={busy || !approved} onClick={() => void request("execute")}>{busy ? "Executing locally…" : "Execute approved SQL"}</button></div>
      </>}
    </div>}
    {result && <section aria-live="polite">
      <div className="section-heading"><span className="step-number" aria-hidden="true">04</span><h2>Results</h2></div><p>{result.answer}</p>
      {!!result.rows.length && <div className="result-table-scroll"><table><thead><tr>{result.columns.map(c => <th scope="col" key={c}>{c}</th>)}</tr></thead><tbody>{result.rows.map((row, i) => <tr key={i}>{result.columns.map(c => <td key={c}>{row[c] === null ? "NULL" : String(row[c])}</td>)}</tr>)}</tbody></table></div>}
      {result.truncated && <p className="notice">Results truncated. Narrow the question to see a smaller slice.</p>}
      <p>Source: <a href={product.source_url} target="_blank" rel="noreferrer">{product.source_name}</a> · Snapshot {product.snapshot_date} · Product v{product.version} · Execution {result.execution_ms.toFixed(0)} ms</p>
      <details><summary>Run trace</summary><ol>{result.trace.map((step, i) => <li key={i}>{step.stage}: {step.result}</li>)}</ol></details>
    </section>}
  </section>;
}
