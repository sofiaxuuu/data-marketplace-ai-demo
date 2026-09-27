"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import SqlWorkflow from "./sql-workflow";

export type InspectProduct = {
  id: string; version: number; name: string; description: string; business_context: string;
  facets: Record<string, string>; coverage: { start: string; end: string; rows: number };
  source_url: string; source_name: string; snapshot_date: string;
  execution_supported: boolean; execution_scope: string; score?: number;
  tables: { id: string; columns: { name: string; type: string; description: string; unit?: string }[] }[];
};

export default function CandidateInspector({ products }: { products: InspectProduct[] }) {
  const [question, setQuestion] = useState("");
  const [candidates, setCandidates] = useState<InspectProduct[] | null>(null);
  const [selection, setSelection] = useState<{ id: string; version: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const selected = products.find(p => p.id === selection?.id && p.version === selection.version);

  useEffect(() => {
    generation.current += 1;
    setSelection(null);
    setCandidates(null);
    setBusy(false);
    setError("");
    return () => { generation.current += 1; };
  }, [products]);

  function changeQuestion(value: string) {
    generation.current += 1;
    setQuestion(value); setSelection(null); setCandidates(null); setError(""); setBusy(false);
  }

  async function search(event: FormEvent) {
    event.preventDefault();
    const requestGeneration = ++generation.current;
    setBusy(true); setError(""); setSelection(null); setCandidates(null);
    try {
      const response = await fetch("/api/retrieval/search", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: question.trim(), top_k: 5 }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Retrieval unavailable");
      if (requestGeneration === generation.current) setCandidates(data.products);
    } catch (cause) {
      if (requestGeneration === generation.current) setError(cause instanceof Error ? cause.message : "Search failed");
    } finally {
      if (requestGeneration === generation.current) setBusy(false);
    }
  }

  return <section className="result-card" aria-label="Human source choice">
    <div className="section-heading"><span className="step-number" aria-hidden="true">01</span><h2>Ask and compare sources</h2></div>
    <p>Similarity is not a decision. Clarify geography, units, adjustment and reporting period, then choose a product to inspect. You can also decide that none fits.</p>
    <form onSubmit={search}>
      <label htmlFor="source-question">Your question</label>
      <textarea id="source-question" value={question} onChange={e => changeQuestion(e.target.value)} rows={3} placeholder="For example: How many Americans were unemployed in April 2020?" />
      <div className="form-footer">
        <button type="button" className="example" onClick={() => changeQuestion("What was the U.S. unemployment rate in April 2020?")}>Try an example: U.S. unemployment in April 2020</button>
        <button className="primary" disabled={busy || !question.trim() || !products.length}>{busy ? "Ranking candidates…" : "Find candidates"}</button>
      </div>
    </form>
    {error && <p role="alert" className="notice error">{error}</p>}
    {candidates !== null && <div aria-live="polite">
      <h3>Top candidates — please choose</h3>
      {!candidates.length && <p>No indexed candidates returned. Browse the catalog below; this is not proof that no source fits.</p>}
      <ul className="candidate-list">{candidates.map(p => <li key={`${p.id}:${p.version}`}>
        <button type="button" onClick={() => setSelection({ id: p.id, version: p.version })}>{p.name}</button>
        <p>{p.description}</p>
        <p>{["entity", "geography", "unit", "seasonal_adjustment", "price_basis", "frequency"].filter(key => p.facets[key]).map(key => p.facets[key]).join(" · ")}</p>
        <small>{p.coverage.start}–{p.coverage.end} · Similarity {p.score?.toFixed(3)} · SQL available after confirmation</small>
      </li>)}</ul>
    </div>}
    <details><summary>Browse all {products.length} local products</summary>
      <ul className="candidate-list">{products.map(p => <li key={`${p.id}:${p.version}`}><button type="button" onClick={() => setSelection({ id: p.id, version: p.version })}>{p.name}</button></li>)}</ul>
    </details>
    {selection && !selected && <p role="alert">This candidate version is no longer in the catalog. Please search again.</p>}
    {selected && <article className="source-inspection" aria-live="polite">
      <div className="section-heading"><span className="step-number" aria-hidden="true">02</span><h2>Review and confirm the source</h2></div>
      <h3>{selected.name}</h3><p>{selected.business_context}</p>
      <dl>{Object.entries(selected.facets).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value}</dd></div>)}</dl>
      <p>Version {selected.version} · {selected.coverage.start}–{selected.coverage.end} · {selected.coverage.rows} rows · Snapshot {selected.snapshot_date}</p>
      <p>Inspect the schema below, then confirm the source to generate a query. Nothing executes until you approve the SQL.</p>
      <a href={selected.source_url} target="_blank" rel="noreferrer">View original source ↗</a>
      <details className="schema-details"><summary>Inspect schema and fields</summary>{selected.tables.map(table => <div key={table.id}><h4>Schema: {table.id}</h4><ul>{table.columns.map(col => <li key={col.name}><code>{col.name}</code> ({col.type}{col.unit ? `, ${col.unit}` : ""}) — {col.description}</li>)}</ul></div>)}</details>
      <button type="button" onClick={() => setSelection(null)}>Choose another source</button>
      <SqlWorkflow key={`${selected.id}:${selected.version}:${question}`} product={selected} question={question} />
    </article>}
  </section>;
}
