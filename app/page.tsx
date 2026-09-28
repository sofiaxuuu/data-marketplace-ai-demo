"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { InspectProduct } from "./candidate-inspector";
import SiteHeader from "./site-header";
import { useCatalog } from "./use-catalog";

export default function Home() {
  const { products, error: catalogError } = useCatalog();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<InspectProduct[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const searchRevision = useRef(0);
  const detailClose = useRef<HTMLButtonElement>(null);
  const detailOpener = useRef<HTMLButtonElement>(null);
  const selected = products.find(product => product.id === selectedId);
  const displayed = results?.filter(result => products.some(product => product.id === result.id && product.version === result.version)) ?? products;

  useEffect(() => {
    if (!selectedId) return;
    detailClose.current?.focus();
    const onDialogKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setSelectedId(null); return; }
      if (event.key !== "Tab") return;
      const controls = Array.from(document.querySelectorAll<HTMLElement>(".product-drawer button, .product-drawer a, .product-drawer summary"));
      if (!controls.length) return;
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onDialogKey);
    return () => { window.removeEventListener("keydown", onDialogKey); detailOpener.current?.focus(); };
  }, [selectedId]);

  async function search(event: FormEvent) {
    event.preventDefault();
    const term = query.trim();
    const revision = ++searchRevision.current;
    if (!term) { setResults(null); setSearchError(""); return; }
    setSearching(true); setSearchError(""); setSelectedId(null);
    try {
      const response = await fetch("/api/retrieval/search", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: term, top_k: 10 }), signal: AbortSignal.timeout(30000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Search unavailable");
      if (revision === searchRevision.current) setResults(data.products);
    } catch (problem) {
      if (revision === searchRevision.current) {
        setResults(null);
        setSearchError(problem instanceof Error ? problem.message : "Search unavailable");
      }
    } finally { if (revision === searchRevision.current) setSearching(false); }
  }

  return <main className="shell">
    <SiteHeader active="marketplace" />
    <div className="marketplace-shell">
      <div className="intro marketplace-intro">
        <p className="eyebrow">DATA MARKETPLACE</p>
        <h1>Find the right data.</h1>
        <p>Explore local data products and their coverage. When you have an analysis question, Ask DataScout will guide you through source choice, SQL review, and results.</p>
      </div>
      <div className="marketplace-search-row">
        <form className="marketplace-search" onSubmit={search} role="search">
          <label htmlFor="marketplace-query">Search datasets by topic, measure, or geography</label>
          <div><input id="marketplace-query" type="search" value={query} onChange={event => {
            setQuery(event.target.value);
            searchRevision.current += 1;
            setSearching(false);
            setResults(null);
            setSelectedId(null);
            setSearchError("");
          }} placeholder="For example: unemployment rate in the United States" />
          <button className="primary" disabled={searching || !query.trim()}>{searching ? "Searching…" : "Search datasets"}</button></div>
        </form>
        <a className="secondary marketplace-ask" href={query.trim() ? `/analyze?q=${encodeURIComponent(query.trim())}` : "/analyze"}>Ask a question →</a>
      </div>
      <p className="search-explanation">Search ranks catalog metadata semantically. It does not start an analysis or call the SQL planner.</p>
      {catalogError && <p role="alert" className="notice error">{catalogError}</p>}
      {searchError && <p role="alert" className="notice error">{searchError} You can still browse all products below.</p>}
      <section className="marketplace-results" aria-label="Local datasets">
        <div className="marketplace-results-heading"><div><p className="eyebrow">LOCAL CATALOG</p><h2>{results ? `${displayed.length} semantic ${displayed.length === 1 ? "match" : "matches"}` : `${products.length} data ${products.length === 1 ? "product" : "products"}`}</h2></div>
          {results && <button className="secondary" onClick={() => { searchRevision.current += 1; setResults(null); setQuery(""); setSearchError(""); }}>Show all products</button>}</div>
        {!products.length && !catalogError && <p>Loading the catalog…</p>}
        {results && !displayed.length && <p>No current catalog products matched this search. Try another term or browse all products.</p>}
        {!!displayed.length && <div className="marketplace-table-scroll"><table className="marketplace-table"><thead><tr><th scope="col">Data product</th><th scope="col">What it contains</th><th scope="col">Coverage</th><th scope="col">Source</th></tr></thead>
          <tbody>{displayed.map(product => <tr key={`${product.id}:${product.version}`}><td><button className="product-link" onClick={event => { detailOpener.current = event.currentTarget; setSelectedId(product.id); }}>{product.name}</button>{results && <small>Semantic match</small>}</td><td>{product.description}</td><td>{product.coverage.start}–{product.coverage.end}</td><td>{product.source_name}</td></tr>)}</tbody></table></div>}
      </section>
      {selected && <><button className="product-backdrop" aria-label="Close dataset details" onClick={() => setSelectedId(null)} /><section className="product-detail product-drawer" role="dialog" aria-modal="true" aria-labelledby="product-detail-title"><div className="detail-heading"><div><p className="eyebrow">DATA PRODUCT · VERSION {selected.version}</p><h2 id="product-detail-title">{selected.name}</h2></div><button ref={detailClose} className="secondary" onClick={() => setSelectedId(null)}>Close</button></div>
        <p>{selected.business_context}</p><dl>{Object.entries(selected.facets).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{value}</dd></div>)}</dl>
        <p>Coverage: {selected.coverage.start}–{selected.coverage.end} · {selected.coverage.rows} rows · Snapshot {selected.snapshot_date}</p>
        <details><summary>Inspect schema and fields</summary>{selected.tables.map(table => <div key={table.id}><h3>{table.id}</h3><ul>{table.columns.map(column => <li key={column.name}><code>{column.name}</code> ({column.type}{column.unit ? `, ${column.unit}` : ""}) — {column.description}</li>)}</ul></div>)}</details>
        <div className="workflow-actions"><a className="primary" href={`/analyze?product_id=${encodeURIComponent(selected.id)}`}>Analyze with this dataset →</a><a href={selected.source_url} target="_blank" rel="noreferrer">Original source ↗</a></div>
      </section></>}
    </div>
  </main>;
}
