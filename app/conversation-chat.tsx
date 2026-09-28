"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { InspectProduct } from "./candidate-inspector";
import type { Workflow } from "./use-workflow";
import SqlWorkflow from "./sql-workflow";

type Conversation = { id: string; title: string; revision: number; product_id: string | null;
  product_version: number | null; active_run_id: string | null };
type Event = { id: number; event_key: string; kind: string; payload: Record<string, unknown> };
type Detail = { conversation: Conversation; events: Event[]; run: Workflow | null };

async function json(response: Response) {
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail ?? "Request failed");
  return value;
}

export default function ConversationChat({ id, products, initialQuestion = "" }: { id: string; products: InspectProduct[]; initialQuestion?: string }) {
  const router = useRouter();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [question, setQuestion] = useState(initialQuestion);
  const [sourceConfirmed, setSourceConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const newConversationKey = useRef<string | null>(null);
  const run = detail?.run;
  const conversation = detail?.conversation;
  const selected = run?.selected;
  const can = (name: string) => !!run && run.status === "ready" && run.allowed_actions.includes(name) && !busy;

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    try {
      const data = await json(await fetch(`/api/conversations/${id}`, { cache: "no-store", signal: AbortSignal.timeout(15000) }));
      if (generation.current === current) { setDetail(data); setError(""); }
    } catch (problem) {
      if (generation.current === current) setError(problem instanceof Error ? problem.message : "Conversation unavailable");
    }
  }, [id]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setSourceConfirmed(false); }, [run?.id, run?.selected?.id, run?.selected?.version]);
  useEffect(() => {
    if (run?.status !== "running") return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [run?.status, refresh]);

  async function perform(operation: () => Promise<Response>): Promise<boolean> {
    if (busy) return false;
    setBusy(true); setError("");
    try { await json(await operation()); await refresh(); return true; }
    catch (problem) { setError(problem instanceof Error ? problem.message : "Action failed"); await refresh(); return false; }
    finally { setBusy(false); }
  }

  function act(action: object) {
    if (!run) return;
    void perform(() => fetch(`/api/workflows/${run.id}/actions`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ request_id: crypto.randomUUID(), expected_revision: run.revision, action }),
      signal: AbortSignal.timeout(190000),
    }));
  }

  function send() {
    if (!conversation || !question.trim()) return;
    const value = question.trim();
    const revising = run && can("revise_question");
    void perform(() => fetch(revising ? `/api/workflows/${run.id}/actions` : `/api/conversations/${id}/turns`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(revising
        ? { request_id: crypto.randomUUID(), expected_revision: run.revision,
            action: { type: "revise_question", question: value } }
        : { request_id: crypto.randomUUID(), expected_revision: conversation.revision, question: value }),
      signal: AbortSignal.timeout(190000),
    })).then(success => { if (success) setQuestion(""); });
  }

  async function startWithProduct(product: InspectProduct) {
    if (run && !run.confirmed && can("select_source")) { act({ type: "select_source", product_id: product.id, manifest_version: product.version }); return; }
    if (busy) return;
    setBusy(true);
    newConversationKey.current ??= crypto.randomUUID();
    try {
      const data = await json(await fetch("/api/conversations", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ request_id: newConversationKey.current, product_id: product.id, manifest_version: product.version }) }));
      router.push(`/analyze/${data.conversation.id}`);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Could not open dataset"); newConversationKey.current = null; setBusy(false); }
  }

  function remove() {
    if (!window.confirm("Delete this conversation? This cannot be undone.")) return;
    setBusy(true);
    const params = new URLSearchParams({ request_id: crypto.randomUUID(), expected_revision: String(conversation?.revision ?? 0) });
    void fetch(`/api/conversations/${id}?${params}`, { method: "DELETE" }).then(json).then(() => router.push("/analyze"))
      .catch(problem => setError(problem instanceof Error ? problem.message : "Deletion failed"))
      .finally(() => setBusy(false));
  }

  const currentTurnPrefix = run && run.turn_index ? `${run.id}:turn:${run.turn_index}:` : "";
  const history = detail?.events.filter(event => !currentTurnPrefix || !event.event_key.startsWith(currentTurnPrefix)) ?? [];
  const askable = !run || can("ask_question") || can("revise_question") || run.status === "ready" && run.stage === "external_review";

  return <div className="conversation-layout">
    <aside className="conversation-sidebar">
      <a href="/analyze" className="secondary">← Conversations</a>
      <h2>{conversation?.title ?? "Conversation"}</h2>
      {conversation?.product_id && <p>Pinned catalog product: {products.find(p => p.id === conversation.product_id)?.name ?? conversation.product_id} · v{conversation.product_version}</p>}
      <details><summary>Choose a dataset</summary><p>Changing a confirmed dataset starts a new conversation.</p>
        <ul className="dataset-picker">{products.map(product => <li key={`${product.id}:${product.version}`}>
          <button type="button" disabled={busy} onClick={() => void startWithProduct(product)}>{product.name}</button>
        </li>)}</ul></details>
      <button type="button" className="secondary" onClick={remove} disabled={busy}>Delete conversation</button>
    </aside>
    <section className="conversation-main" aria-label="Analysis conversation">
      <header><p className="eyebrow">ASK DATASCOUT</p><h1>{conversation?.title ?? "Loading conversation…"}</h1>
        <p>Choose and confirm one dataset, review SQL for each analytical question, then approve execution.</p></header>
      {!!history.length && <div className="conversation-history">{history.map(event => <EventCard key={event.id} event={event} />)}</div>}
      {run && <div className="conversation-current" aria-live="polite">
        <p className="eyebrow">CURRENT WORKFLOW · {run.status === "running" ? "WORKING" : run.stage.replaceAll("_", " ").toUpperCase()}</p>
        <p className="conversation-user-question">{run.analysis_question ?? run.question}</p>
        {run.status === "running" && <p role="status" className="notice">Working on this step…</p>}
        {run.error && <p role="alert" className="notice error">{run.error}</p>}
        {run.advice && <section><h2>{run.advice.outcome === "recommend" ? "Recommended datasets" : run.advice.outcome === "clarify" ? "Clarify your question" : "No suitable local dataset identified"}</h2>
          <p>{run.advice.reason}</p>{run.advice.clarification && <p>{run.advice.clarification}</p>}
          <div className="conversation-cards">{run.advice.recommendations.map(rec => <article key={rec.product.id}>
            <h3>{rec.product.name}</h3><p>{rec.reason}</p><p>{rec.product.coverage.start}–{rec.product.coverage.end} · {rec.product.facets.unit ?? "Units vary"}</p>
            {!!rec.caveats.length && <p>Check: {rec.caveats.join(" · ")}</p>}
            <button disabled={!can("select_source")} onClick={() => act({ type: "select_source", product_id: rec.product.id, manifest_version: rec.product.version })}>Review this dataset</button>
          </article>)}</div>
          {can("none_fit") && <button className="secondary" onClick={() => act({ type: "none_fit" })}>None of these fit</button>}
        </section>}
        {can("discover_external") && <section className="conversation-action"><h2>Look beyond the catalog</h2>
          <p>Find external sources sends this question to Exa. DataScout recommends source pages; it does not acquire data.</p>
          <button onClick={() => act({ type: "discover_external", consent: true })}>Find external sources</button></section>}
        {run.external && <section><h2>Where to find the data</h2>
          <p>{run.external.length ? run.external_advice?.answer ?? "Review these source pages and their evidence." : "No external candidates were returned. Try a more specific source question; this does not prove no dataset exists."}</p>
          {!!run.external_advice?.unresolved.length && <p>Still to verify: {run.external_advice.unresolved.join(" · ")}</p>}
          <div className="conversation-cards">{run.external.map((item, index) => <article key={item.url}>
            <h3><a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a></h3><small>{item.publisher} · {item.provider} · External source, not in the catalog</small>
            {run.external_advice?.assessments.filter(assessment => assessment.candidate_index === index).map(assessment =>
              <p key={assessment.candidate_index}><strong>{assessment.fit} fit:</strong> {assessment.reason} {assessment.caveat}</p>)}
            <p>Evidence: {item.evidence}</p><p>Coverage: {item.coverage} · Units: {item.units}</p>
          </article>)}</div>
        </section>}
        {selected && !run.confirmed && run.stage === "source_review" ? <section className="conversation-source-review">
          <h2>Review this dataset</h2><h3>{selected?.name}</h3><p>{selected?.business_context}</p>
          <p>Coverage: {selected?.coverage.start}–{selected?.coverage.end} · {selected?.coverage.rows} rows</p>
          <details><summary>Inspect fields</summary><ul>{selected?.tables.flatMap(t => t.columns).map(column => <li key={column.name}>{column.name} ({column.type}) — {column.description}</li>)}</ul></details>
          <p>Confirming this source {run.intent === "source_finding" ? "lets you ask a separate analytical question" : "will generate SQL for the question above"}.</p>
          <label><input type="checkbox" checked={sourceConfirmed} onChange={event => setSourceConfirmed(event.target.checked)} /> I confirm this dataset fits the intended analysis.</label>
          <p><button disabled={!sourceConfirmed || !can("confirm_source")} onClick={() => act({ type: "confirm_source", confirmed: true })}>Confirm dataset</button></p>
        </section> : null}
        {run.stage === "question_review" && <section><h2>Interpretation for this turn</h2><p>{run.interpreted_question}</p>
          <p>Confirm the measure, geography, and period before SQL is generated.</p>
          <button disabled={!can("confirm_interpretation")} onClick={() => act({ type: "confirm_interpretation", confirmed: true })}>Generate SQL for this interpretation</button>
        </section>}
        {run.stage === "question_clarification" && <p className="notice">{run.interpretation_clarification} Ask a clearer question below.</p>}
        {selected && ["sql_review", "sql_abstain", "sql_clarification"].includes(run.stage) && <><p><strong>Interpreted question:</strong> {run.interpreted_question ?? run.analysis_question ?? run.question}</p>
          <SqlWorkflow run={run} busy={busy} step={3} onAction={act} /></>}
        {selected && run.stage === "results" && <SqlWorkflow run={run} busy={busy} step={4} onAction={act} />}
        {can("retry") && <button className="secondary" onClick={() => act({ type: "retry" })}>Retry failed step</button>}
        {!!run.trace.length && <details><summary>Workflow trace</summary><ol>{run.trace.map((entry, index) => <li key={index}>{entry.stage}: {entry.status} — {entry.result}</li>)}</ol></details>}
      </div>}
      <form className="conversation-composer" onSubmit={event => { event.preventDefault(); send(); }}>
        <label htmlFor="conversation-question">{selected?.name ?? conversation?.product_id ? "Ask an analytical question about this dataset" : run?.allowed_actions.includes("revise_question") ? "Clarify or revise your source question" : "Ask DataScout to find data or answer a question"}</label>
        <textarea id="conversation-question" rows={3} value={question} onChange={event => setQuestion(event.target.value)}
          placeholder={selected?.name || conversation?.product_id ? "For example: Which date had the highest value?" : "For example: Where can I find daily PM2.5 measurements for Seattle in 2024?"} />
        <button className="primary" disabled={!askable || busy || !question.trim()}>Send question</button>
        {run && !askable && <small>Complete the current review or approval before sending another question.</small>}
      </form>
      {error && <p role="alert" className="notice error">{error} <button onClick={() => void refresh()}>Refresh</button></p>}
    </section>
  </div>;
}

function EventCard({ event }: { event: Event }) {
  const payload = event.payload;
  if (event.kind === "question") return <article className="conversation-event user"><small>{payload.purpose === "source_finding" ? "Source question" : "Analysis question"}</small><p>{String(payload.text)}</p></article>;
  if (event.kind === "interpretation") return <article className="conversation-event"><small>Interpreted question</small><p>{String(payload.question)}</p></article>;
  if (event.kind === "approved_sql") return <article className="conversation-event"><small>Approved SQL</small><details><summary>Inspect query</summary><pre>{String(payload.sql)}</pre></details></article>;
  if (event.kind === "result") {
    const result = payload as { answer?: string; rows?: Record<string, unknown>[]; columns?: string[]; truncated?: boolean };
    return <article className="conversation-event"><small>Completed answer</small><p>{result.answer}</p>
      {!!result.rows?.length && <details><summary>Inspect {result.rows.length} saved result rows</summary><div className="result-table-scroll"><table><thead><tr>{result.columns?.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{result.rows.map((row, index) => <tr key={index}>{result.columns?.map(column => <td key={column}>{String(row[column] ?? "NULL")}</td>)}</tr>)}</tbody></table></div></details>}
      {result.truncated && <p>Result was truncated.</p>}</article>;
  }
  if (event.kind === "source") return <article className="conversation-event"><small>Confirmed dataset</small><p>{String(payload.name)} · {String((payload.coverage as Record<string, unknown>)?.start)}–{String((payload.coverage as Record<string, unknown>)?.end)}</p></article>;
  if (event.kind === "source_advice") {
    const recommendations = (payload.recommendations ?? []) as { product?: { name?: string; coverage?: { start: string; end: string } }; reason?: string; caveats?: string[] }[];
    return <article className="conversation-event"><small>Local source advice</small><p>{String(payload.reason)}</p>
      <ul>{recommendations.map((rec, index) => <li key={index}>{rec.product?.name}: {rec.reason} ({rec.product?.coverage?.start}–{rec.product?.coverage?.end})</li>)}</ul></article>;
  }
  if (event.kind === "external_sources") {
    const recommendation = payload.recommendation as { answer?: string } | null;
    const candidates = (payload.candidates ?? []) as { title: string; url: string; publisher: string; evidence: string }[];
    return <article className="conversation-event"><small>External source discovery</small><p>{recommendation?.answer ?? "Sources were reviewed."}</p>
      <details><summary>Source links and evidence</summary><ul>{candidates.map(item => <li key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a> · {item.publisher}<p>{item.evidence}</p></li>)}</ul></details></article>;
  }
  if (event.kind === "error") return <article className="conversation-event"><small>Workflow error</small><p>{String(payload.message)}</p></article>;
  return null;
}
