"use client";

import { FormEvent, useEffect, useRef, useState } from "react";

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
    <h2>Compare and choose a source</h2>
    <p>Similarity is not a decision. Clarify geography, units, adjustment and reporting period, then choose a product to inspect. You can also decide that none fits.</p>
    <form onSubmit={search}>
      <label htmlFor="source-question">What data do you need?</label>
      <textarea id="source-question" value={question} onChange={e => changeQuestion(e.target.value)} rows={3} placeholder="For example: How many Americans were unemployed in April 2020?" />
      <button className="primary" disabled={busy || !question.trim() || !products.length}>{busy ? "Ranking candidates…" : "Find candidates"}</button>
    </form>
    {error && <p role="alert" className="notice error">{error}</p>}
    {candidates !== null && <div aria-live="polite">
      <h3>Top candidates — please choose</h3>
      {!candidates.length && <p>No indexed candidates returned. Browse the catalog below; this is not proof that no source fits.</p>}
      <ul className="candidate-list">{candidates.map(p => <li key={`${p.id}:${p.version}`}>
        <button type="button" onClick={() => setSelection({ id: p.id, version: p.version })}>{p.name}</button>
        <p>{p.description}</p>
        <p>{["entity", "geography", "unit", "seasonal_adjustment", "price_basis", "frequency"].filter(key => p.facets[key]).map(key => p.facets[key]).join(" · ")}</p>
        <small>{p.coverage.start}–{p.coverage.end} · Similarity {p.score?.toFixed(3)} · {p.execution_scope}</small>
      </li>)}</ul>
    </div>}
    <details><summary>Browse all {products.length} local products</summary>
      <ul className="candidate-list">{products.map(p => <li key={`${p.id}:${p.version}`}><button type="button" onClick={() => setSelection({ id: p.id, version: p.version })}>{p.name}</button></li>)}</ul>
    </details>
    {selection && !selected && <p role="alert">This candidate version is no longer in the catalog. Please search again.</p>}
    {selected && <article className="source-inspection" aria-live="polite">
      <h3>{selected.name}</h3><p>{selected.business_context}</p>
      <dl>{Object.entries(selected.facets).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value}</dd></div>)}</dl>
      <p>Version {selected.version} · {selected.coverage.start}–{selected.coverage.end} · {selected.coverage.rows} rows · Snapshot {selected.snapshot_date}</p>
      <p>{selected.execution_scope}. Inspection does not execute a query.</p>
      <a href={selected.source_url} target="_blank" rel="noreferrer">View original source ↗</a>
      {selected.tables.map(table => <div key={table.id}><h4>Schema: {table.id}</h4><ul>{table.columns.map(col => <li key={col.name}><code>{col.name}</code> ({col.type}{col.unit ? `, ${col.unit}` : ""}) — {col.description}</li>)}</ul></div>)}
      <button type="button" onClick={() => setSelection(null)}>Clear choice</button>
    </article>}
  </section>;
}
