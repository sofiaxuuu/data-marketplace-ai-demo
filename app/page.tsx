"use client";

import { FormEvent, useEffect, useState } from "react";

type TraceStep = { stage: string; result: string };
type Product = {
  id: string;
  version: number;
  name: string;
  description: string;
  coverage: string;
  coverage_range: string;
  source_url: string;
  snapshot_date: string;
};
type Proposal = {
  outcome: "selected" | "abstain";
  reason: string;
  product?: Product;
  candidates?: { id: string; name: string }[];
  retrieved_products?: { id: string; name: string; score: number }[];
  selected_fields?: string[];
  trace: TraceStep[];
};
type Answer = {
  outcome: "answered";
  answer: string;
  sql: string;
  rows: Record<string, string | number>[];
  product: Product;
  selected_fields: string[];
  trace: TraceStep[];
};

const example = "What was the U.S. unemployment rate in April 2020?";
type CatalogProduct = { id: string; version: number; name: string };

async function post<T>(path: string, body: object): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? "The request failed.");
  return data as T;
}

export default function Home() {
  const [question, setQuestion] = useState("");
  const [submittedQuestion, setSubmittedQuestion] = useState("");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [busy, setBusy] = useState<"preview" | "execute" | null>(null);
  const [error, setError] = useState("");
  const [catalog, setCatalog] = useState<CatalogProduct[]>([]);

  useEffect(() => {
    fetch("/api/catalog")
      .then((response) => (response.ok ? response.json() : []))
      .then((items: CatalogProduct[]) => setCatalog(items))
      .catch(() => setCatalog([]));
  }, []);

  async function analyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!question.trim()) return;
    setBusy("preview");
    setError("");
    setProposal(null);
    setAnswer(null);
    setSubmittedQuestion(question.trim());
    try {
      setProposal(await post<Proposal>("/api/runs/preview", { question: question.trim() }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The request failed.");
    } finally {
      setBusy(null);
    }
  }

  async function confirm() {
    if (!proposal?.product) return;
    setBusy("execute");
    setError("");
    try {
      const result = await post<Answer>("/api/runs/execute", {
          question: submittedQuestion,
          product_id: proposal.product.id,
          manifest_version: proposal.product.version,
        });
      setAnswer({ ...result, trace: [
        ...proposal.trace.filter((step) => step.stage === "SingleStore vector retrieval"),
        ...result.trace,
      ] });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The request failed.");
    } finally {
      setBusy(null);
    }
  }

  const trace = answer?.trace ?? proposal?.trace ?? [];

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">D<span>·</span></span><span>DataScout</span></div>
        <span className="edition">LOCAL DATA LAB / V1 SLICE</span>
      </header>

      <div className="workspace">
        <section className="main-column" aria-label="Analysis workspace">
          <div className="intro">
            <p className="eyebrow">ASK THE DATA</p>
            <h1>Find the source. Then ask the question.</h1>
            <p>Review the proposed dataset before DataScout runs a query against its local snapshot.</p>
          </div>

          <form onSubmit={analyze} className="question-card">
            <label htmlFor="question">Your question</label>
            <textarea
              id="question"
              value={question}
              onChange={(event) => {
                setQuestion(event.target.value);
                setProposal(null);
                setAnswer(null);
              }}
              placeholder="Ask about a measure and a month…"
              rows={3}
            />
            <div className="form-footer">
              <button type="button" className="example" onClick={() => {
                setQuestion(example);
                setProposal(null);
                setAnswer(null);
              }}>
                Try: U.S. unemployment in April 2020
              </button>
              <button type="submit" className="primary" disabled={busy !== null || !question.trim()}>
                {busy === "preview" ? "Checking sources…" : "Find source"}
              </button>
            </div>
          </form>

          {error && <div role="alert" className="notice error">{error}</div>}

          {proposal && (
            <section className="result-card" aria-live="polite">
              <div className="section-heading"><span className="step-number">01</span><h2>Source review</h2></div>
              {proposal.retrieved_products && <div className="retrieved-products">
                <p className="eyebrow">RETRIEVED CANDIDATES</p>
                <ol>{proposal.retrieved_products.map((item) => <li key={item.id}><span>{item.name}</span><span className="similarity">Similarity {item.score.toFixed(3)}</span></li>)}</ol>
                <p className="provenance">Similarity ranks relevance. Measure and date coverage are checked separately.</p>
              </div>}
              {proposal.outcome === "abstain" ? (
                <div className="notice"><strong>No suitable source yet</strong><p>{proposal.reason}</p></div>
              ) : proposal.product ? (
                <>
                  <div className="source-title"><span className="source-icon">{proposal.product.id === "fred_unemployment" ? "F" : "W"}</span><div><h3>{proposal.product.name}</h3><p>{proposal.product.description}</p></div></div>
                  <p className="reason">{proposal.reason}</p>
                  <div className="facts">
                    <div><span>Coverage</span><strong>{proposal.product.coverage_range}</strong></div>
                    <div><span>Snapshot taken</span><strong>{proposal.product.snapshot_date}</strong></div>
                    <div><span>Fields</span><strong>{proposal.selected_fields?.join(", ")}</strong></div>
                  </div>
                  <div className="review-footer">
                    <a href={proposal.product.source_url} target="_blank" rel="noreferrer">View source ↗</a>
                    <button className="primary" onClick={confirm} disabled={busy !== null || !!answer}>
                      {busy === "execute" ? "Running query…" : answer ? "Source confirmed" : "Confirm & run query"}
                    </button>
                  </div>
                </>
              ) : null}
            </section>
          )}

          {answer && (
            <section className="result-card answer-card" aria-live="polite">
              <div className="section-heading"><span className="step-number">02</span><h2>Answer</h2></div>
              <p className="answer-text">{answer.answer}</p>
              <p className="provenance">Source: {answer.product.name} · Local snapshot taken {answer.product.snapshot_date}</p>
              <details className="sql-panel"><summary>View generated SQL</summary><pre><code>{answer.sql}</code></pre></details>
            </section>
          )}
        </section>

        <aside className="side-column" aria-label="Run details">
          <div className="side-card">
            <p className="eyebrow">CURRENT CATALOG</p>
            <h2>{catalog.length} local {catalog.length === 1 ? "product" : "products"}</h2>
            {catalog.length ? <ul className="catalog-list">{catalog.map((item) => <li key={item.id}>{item.name}</li>)}</ul> : <p>Catalog unavailable until the API starts.</p>}
            <div className="catalog-note">Real observations saved as local Parquet snapshots. Apple SEC metadata is searchable; its question-execution adapter is next.</div>
          </div>
          <div className="side-card trace-card">
            <p className="eyebrow">RUN TRACE</p>
            {trace.length ? (
              <ol>{trace.map((item, index) => <li key={`${item.stage}-${index}`}><span>{item.stage}</span><p>{item.result}</p></li>)}</ol>
            ) : <p>Find a source to see each decision and query step here.</p>}
          </div>
        </aside>
      </div>
    </main>
  );
}
