"use client";

import { useRef } from "react";
import SqlWorkflow from "./sql-workflow";
import { useWorkflow } from "./use-workflow";

export type InspectProduct = {
  id: string; version: number; name: string; description: string; business_context: string;
  facets: Record<string, string>; coverage: { start: string; end: string; rows: number };
  source_url: string; source_name: string; snapshot_date: string;
  execution_supported: boolean; execution_scope: string; score?: number;
  tables: { id: string; columns: { name: string; type: string; description: string; unit?: string }[] }[];
};

export default function CandidateInspector({ products }: { products: InspectProduct[] }) {
  const flow = useWorkflow(products);
  const { run, question, busy, error } = flow;
  const browse = useRef<HTMLDetailsElement>(null);
  const selected = run?.selected;
  const advice = run?.advice;
  const can = (action: string) => !busy && !!run?.allowed_actions.includes(action);
  const openBrowse = () => { if (browse.current) browse.current.open = true; browse.current?.scrollIntoView?.({ block: "nearest" }); };
  const externalOffered = !!run?.allowed_actions.includes("discover_external");

  return <section className="result-card" aria-label="Human source choice">
    <div className="section-heading"><span className="step-number" aria-hidden="true">01</span><h2>Ask and compare sources</h2></div>
    <p>The Source Advisor checks all local products’ definitions, fields and coverage. Recommendations are proposals—not automatic source choices.</p>
    <p className="catalog-note">Finding candidates sends your question and catalog metadata/schema to OpenAI, not sample rows. Browse locally to choose a source without an advisor call.</p>
    <form onSubmit={e => { e.preventDefault(); void flow.start(); }}>
      <label htmlFor="source-question">Your question</label>
      <textarea id="source-question" value={question} onChange={e => flow.changeQuestion(e.target.value)} rows={3} placeholder="For example: How many Americans were unemployed in April 2020?" />
      <div className="form-footer">
        <button type="button" className="example" onClick={() => flow.changeQuestion("What was the U.S. unemployment rate in April 2020?")}>Try an example: U.S. unemployment in April 2020</button>
        <button className="primary" disabled={busy || !question.trim() || !products.length}>{busy ? "Working…" : "Find candidates"}</button>
      </div>
    </form>
    {busy && <p role="status">Waiting for the current workflow…</p>}
    {error && <p role="alert" className="notice error">{error} <button type="button" onClick={() => void flow.refresh()}>Refresh workflow state</button></p>}
    {run?.error && <p role="alert" className="notice error">{run.error}</p>}
    {run?.allowed_actions.includes("retry") && <button type="button" disabled={busy} onClick={() => void flow.act({ type: "retry" })}>Retry failed step{run.trace.at(-1)?.stage === "discovery" ? " (sends question to Exa again)" : ""}</button>}
    {advice && <div aria-live="polite">
      <h3>{advice.outcome === "recommend" ? "Recommended local sources — please choose" : advice.outcome === "clarify" ? "Please clarify your question" : advice.limitation === "unsupported_operation" ? "This operation is not supported" : "No suitable local product identified"}</h3>
      <p>{advice.reason}</p>
      {advice.outcome === "clarify" && <p className="notice">{advice.clarification} Update the question above, then find candidates again.</p>}
      {advice.limitation === "unsupported_operation" && <p>A new dataset may not fix this SQL limitation. Simplify the operation or browse local products.</p>}
      <ul className="candidate-list">{advice.recommendations.map(rec => {
        const p = rec.product;
        return <li key={p.id + ":" + p.version}><h4>{p.name}</h4><p>{rec.reason}</p>
          <p>{["entity", "geography", "unit", "seasonal_adjustment", "price_basis", "frequency"].filter(k => p.facets[k]).map(k => p.facets[k]).join(" · ")}</p>
          <small>{p.coverage.start}–{p.coverage.end} · SQL available after confirmation</small>
          {!!rec.caveats.length && <ul className="caveats">{rec.caveats.map((c, i) => <li key={i}>{c}</li>)}</ul>}
          <button type="button" aria-label={"Review this dataset: " + p.name} disabled={!can("select_source")} onClick={() => void flow.select(p)}>Review this dataset</button>
        </li>;
      })}</ul>
    </div>}
    {run?.allowed_actions.includes("none_fit") && <button type="button" className="secondary" disabled={busy} onClick={() => void flow.act({ type: "none_fit" })}>None of these fit</button>}
    {externalOffered && <section className="external-discovery" aria-label="External source discovery">
      <h3>Look beyond the local catalog</h3>
      <p>Find external sources sends your question to Exa. Results are candidate links—not available for SQL. Nothing is acquired, registered or indexed.</p>
      <div className="workflow-actions"><button type="button" className="primary" disabled={!can("discover_external")} onClick={() => void flow.act({ type: "discover_external", consent: true })}>Find external sources</button>
        <button type="button" className="secondary" onClick={openBrowse}>Browse local catalog</button></div>
    </section>}
    {run?.external && <section aria-label="External candidates" aria-live="polite">
      <h3>External candidate sources</h3>
      {!run.external.length && <p>No external candidates returned. Try a more specific question; this is not proof no source exists.</p>}
      <ul className="candidate-list">{run.external.map(c => <li key={c.url}>
        <h4><a href={c.url} target="_blank" rel="noreferrer">{c.title} ↗</a></h4>
        <small>{c.publisher} · {c.provider} · {c.discovered_at} · Not available for SQL</small>
        <p>{c.evidence}</p><p>{c.relevance}</p>
        <dl>{(["coverage", "units", "access", "licensing"] as const).map(k => <div key={k}><dt>{k}</dt><dd>{c[k]}</dd></div>)}</dl>
      </li>)}</ul>
    </section>}
    <details ref={browse}><summary>Browse all {products.length} local products</summary>
      <ul className="candidate-list">{products.map(p => <li key={p.id + ":" + p.version}><button type="button" disabled={busy || !question.trim() || !!run && !can("select_source")} onClick={() => void flow.select(p)}>{p.name}</button></li>)}</ul>
    </details>
    {selected && <article className="source-inspection" aria-live="polite">
      <div className="section-heading"><span className="step-number" aria-hidden="true">02</span><h2>Review and confirm the source</h2></div>
      <h3>{selected.name}</h3><p>{selected.business_context}</p>
      <dl>{Object.entries(selected.facets).map(([k, v]) => <div key={k}><dt>{k.replaceAll("_", " ")}</dt><dd>{v}</dd></div>)}</dl>
      <p>Version {selected.version} · {selected.coverage.start}–{selected.coverage.end} · {selected.coverage.rows} rows · Snapshot {selected.snapshot_date}</p>
      <a href={selected.source_url} target="_blank" rel="noreferrer">View original source ↗</a>
      <details className="schema-details"><summary>Inspect schema and fields</summary>{selected.tables.map(t => <div key={t.id}><h4>Schema: {t.id}</h4><ul>{t.columns.map(c => <li key={c.name}><code>{c.name}</code> ({c.type}{c.unit ? ", " + c.unit : ""}) — {c.description}</li>)}</ul></div>)}</details>
      <button type="button" disabled={!can("choose_again")} onClick={() => void flow.act({ type: "choose_again" })}>Choose another source</button>
      <SqlWorkflow run={run} busy={busy || !products.length} onAction={action => void flow.act(action)} />
    </article>}
    {run && <details className="workflow-trace"><summary>Agent workflow trace</summary><ol>{run.trace.map((t, i) => <li key={i}>
      {t.stage}: {t.status} — {t.result} · {t.model ?? t.provider ?? "local"} · {t.duration_ms.toFixed(0)} ms
      {t.usage?.total_tokens !== undefined && " · " + t.usage.total_tokens + " tokens"}
    </li>)}</ol><small>LangGraph · Run {run.id} · Revision {run.revision} · {run.stage}</small></details>}
  </section>;
}
