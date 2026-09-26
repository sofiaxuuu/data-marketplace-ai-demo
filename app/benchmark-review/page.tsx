"use client";

import { useEffect, useState } from "react";
import type { InspectProduct } from "../candidate-inspector";

type Outcome = "select" | "clarify" | "abstain";
type Decision = "approve" | "correct" | "rewrite" | "unsure";
type Case = { id: string; question: string; expected_outcome: Outcome; expected_data_products: string[]; label_reason: string };
type Saved = { revision: number; decision: Decision; outcome: Outcome | null; product_ids: string[]; comment: string; stale: boolean; saved_at: string };
type Data = { cases: Case[]; products: InspectProduct[]; reviews: Record<string, Saved>; benchmark_hash: string; lock_hash: string; blocked: string | null };
const outcomes = { select: "Choose one dataset", clarify: "Needs clarification", abstain: "No suitable dataset" };

export default function BenchmarkReview() {
  const [data, setData] = useState<Data | null>(null);
  const [index, setIndex] = useState(0);
  const [unreviewed, setUnreviewed] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const [decision, setDecision] = useState<Decision>("approve");
  const [outcome, setOutcome] = useState<Outcome>("select");
  const [ids, setIds] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [query, setQuery] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const cases = data?.cases.filter(c => !unreviewed || !data.reviews[c.id] || data.reviews[c.id].stale || data.reviews[c.id].decision === "unsure") ?? [];
  const current = cases[index];
  const saved = current && data?.reviews[current.id];

  async function load() {
    setError("");
    try {
      const response = await fetch("/api/benchmark-review", { cache: "no-store" });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail ?? "Review unavailable");
      setData(value); setIndex(0); setDirty(false);
    } catch (e) { setError(e instanceof Error ? e.message : "Review unavailable"); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    setRevealed(false); setDecision(saved?.decision ?? "approve");
    setOutcome(saved?.outcome ?? "select"); setIds(saved?.product_ids ?? []);
    setComment(saved?.comment ?? ""); setQuery(""); setDirty(false);
  // Reset the form when navigating, not while editing.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.id, saved?.revision]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function canLeave() { return !dirty || window.confirm("Discard unsaved review changes?"); }
  function chooseDecision(value: Decision) { setDecision(value); setDirty(true); }

  async function save() {
    if (!data || !current) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const response = await fetch("/api/benchmark-review", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        case_id: current.id, benchmark_hash: data.benchmark_hash, lock_hash: data.lock_hash,
        previous_revision: saved?.revision ?? null, decision, outcome: decision === "correct" ? outcome : null,
        product_ids: decision === "correct" ? ids : [], comment,
      }) });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail ?? "Save failed");
      setData(previous => previous && ({ ...previous, reviews: { ...previous.reviews, [current.id]: value } }));
      setDirty(false); setNotice("Review saved. Benchmark labels and metadata were not changed.");
      if (!unreviewed) setIndex(Math.min(index + 1, cases.length - 1));
      else if (decision !== "unsure") setIndex(Math.min(index, Math.max(0, cases.length - 2)));
    } catch (e) { setError(e instanceof Error ? e.message : "Save failed"); }
    finally { setBusy(false); }
  }

  const reviewed = data?.cases.filter(c => data.reviews[c.id] && !data.reviews[c.id].stale && data.reviews[c.id].decision !== "unsure").length ?? 0;
  const valid = decision !== "correct" || (outcome === "select" ? ids.length === 1 : outcome === "clarify" ? ids.length >= 2 : ids.length === 0);
  const needsComment = decision === "correct" || decision === "rewrite";
  const products = data?.products.filter(p => !query.trim() ? current?.expected_data_products.includes(p.id) || ids.includes(p.id) : `${p.name} ${p.description} ${Object.values(p.facets).join(" ")}`.toLowerCase().includes(query.toLowerCase())) ?? [];

  return <main className="review-shell">
    <a href="/" onClick={e => { if (!canLeave()) e.preventDefault(); }}>← DataScout</a>
    <h1>Benchmark review</h1>
    <p>Does the proposed dataset choice fit the business question? No calculations or queries are run here.</p>
    <p aria-live="polite">{reviewed} of {data?.cases.length ?? 60} reviewed · Shared local review</p>
    <div className="review-actions">
      <label><input type="checkbox" checked={unreviewed} disabled={busy} onChange={e => { if (canLeave()) { setUnreviewed(e.target.checked); setIndex(0); setDirty(false); } }} /> Unreviewed / not sure only</label>
      <button disabled={busy} onClick={() => { if (canLeave()) void load(); }}>Reload reviews</button>
    </div>
    {error && <p role="alert" className="notice error">{error}</p>}
    {notice && <p role="status" className="notice">{notice}</p>}
    {data?.blocked && <p role="alert" className="notice error">{data.blocked} Saving is disabled.</p>}
    {!data && !error && <p>Loading benchmark…</p>}
    {data && !current && <p>No questions remain in this filter. Switch to all questions to revisit saved reviews.</p>}
    {current && <section className="result-card" key={current.id}>
      <p className="eyebrow">Question {index + 1} of {cases.length}</p>
      <h2>{current.question}</h2>
      {saved && <p>{saved.stale ? "Previous review is stale" : `Saved review: ${saved.decision}`} · {new Date(saved.saved_at).toLocaleString()}</p>}
      {!revealed ? <button className="primary" onClick={() => setRevealed(true)}>Reveal benchmark answer</button> : <>
        <section className="review-proposal" aria-label="Benchmark proposal">
          <h3>Agent-authored proposal — please verify</h3>
          <p>{outcomes[current.expected_outcome]}</p>
          {current.expected_data_products.map(id => { const p = data?.products.find(p => p.id === id); return <article key={id}>
            <h4>{p?.name ?? id}</h4><p>{p?.description}</p>
            {p && <p>{Object.values(p.facets).join(" · ")}<br />Coverage: {p.coverage.start}–{p.coverage.end} · <a href={p.source_url} target="_blank" rel="noreferrer">Original source ↗</a></p>}
          </article>; })}
          <p><strong>Why:</strong> {current.label_reason}</p>
        </section>
        <fieldset disabled={busy || !!data?.blocked}>
          <legend>Your review</legend>
          <div className="review-actions">{([['approve', 'Approve'], ['correct', 'Suggest correction'], ['rewrite', 'Question needs rewriting'], ['unsure', 'Not sure']] as [Decision, string][]).map(([value, label]) => <label key={value}><input type="radio" name="decision" checked={decision === value} onChange={() => chooseDecision(value)} /> {label}</label>)}</div>
          {decision === "correct" && <>
            <label htmlFor="review-outcome">What should happen?</label>
            <select id="review-outcome" value={outcome} onChange={e => { setOutcome(e.target.value as Outcome); setIds([]); setDirty(true); }}>{Object.entries(outcomes).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
            {outcome !== "abstain" && <>
              <label htmlFor="product-search">Find a dataset (type to search all {data?.products.length})</label>
              <input id="product-search" type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="GDP, Microsoft, unemployment…" />
              <p>{ids.length} selected{outcome === "clarify" ? " — choose at least two plausible datasets" : " — choose one dataset"}</p>
              <ul className="candidate-list">{products.map(p => <li key={p.id}><label><input type={outcome === "select" ? "radio" : "checkbox"} name="products" checked={ids.includes(p.id)} onChange={() => { setIds(outcome === "select" ? [p.id] : ids.includes(p.id) ? ids.filter(id => id !== p.id) : [...ids, p.id]); setDirty(true); }} /> {p.name}</label><p>{p.description}</p><small>{p.coverage.start}–{p.coverage.end} · {Object.values(p.facets).join(" · ")}</small></li>)}</ul>
              {!products.length && <p>No matching products. Try another search.</p>}
            </>}
          </>}
          <label htmlFor="review-comment">Comment {needsComment ? "(required)" : "(optional)"}</label>
          <textarea id="review-comment" rows={3} maxLength={4000} value={comment} onChange={e => { setComment(e.target.value); setDirty(true); }} />
          <button className="primary" disabled={busy || !valid || (needsComment && !comment.trim())} onClick={() => void save()}>{busy ? "Saving…" : "Save and next"}</button>
        </fieldset>
      </>}
      <nav className="review-actions" aria-label="Question navigation">
        <button disabled={busy || index === 0} onClick={() => { if (canLeave()) setIndex(index - 1); }}>Previous</button>
        <button disabled={busy || index >= cases.length - 1} onClick={() => { if (canLeave()) setIndex(index + 1); }}>Next / skip</button>
      </nav>
    </section>}
  </main>;
}
