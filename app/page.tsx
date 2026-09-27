"use client";

import { useEffect, useState } from "react";
import CandidateInspector, { InspectProduct } from "./candidate-inspector";

export default function Home() {
  const [catalog, setCatalog] = useState<InspectProduct[]>([]);
  const [catalogError, setCatalogError] = useState("");

  useEffect(() => {
    let active = true;
    const refresh = () => fetch("/api/catalog", { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error("Catalog unavailable");
        return response.json();
      })
      .then((items: InspectProduct[]) => {
        if (active) {
          setCatalog(previous => JSON.stringify(previous) === JSON.stringify(items) ? previous : items);
          setCatalogError("");
        }
      })
      .catch(() => {
        if (active) {
          setCatalog(previous => previous.length ? [] : previous);
          setCatalogError("Catalog unavailable. Check that the Python API is running, then try again.");
        }
      });
    refresh();
    const timer = window.setInterval(refresh, 30000);
    window.addEventListener("focus", refresh);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, []);

  return <main className="shell">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">D<span>·</span></span><span>DataScout</span></div>
      <a href="/benchmark-review">Review benchmark →</a>
    </header>
    <div className="workspace">
      <section className="main-column" aria-label="Analysis workspace">
        <div className="intro">
          <p className="eyebrow">ASK THE DATA</p>
          <h1>Ask a question. Choose the source. Review the SQL.</h1>
          <p>Get explained source recommendations, confirm one dataset, then review and approve its query. Discover external sources only when you choose to.</p>
        </div>
        {catalogError && <p role="alert" className="notice error">{catalogError}</p>}
        <CandidateInspector products={catalog} />
      </section>
      <aside className="side-column" aria-label="Catalog details">
        <div className="side-card">
          <p className="eyebrow">CURRENT CATALOG</p>
          <h2>{catalog.length} local {catalog.length === 1 ? "product" : "products"}</h2>
          {catalog.length ? <ul className="catalog-list">{catalog.map(item => <li key={item.id}>{item.name}</li>)}</ul> : <p>{catalogError ? "Catalog unavailable." : "Loading the catalog…"}</p>}
          <div className="catalog-note">All 15 products support the single-product SQL workflow, subject to available fields, periods and supported operations. Real observations are local Parquet snapshots. External discovery returns candidate links only; no live data acquisition or cross-product joins.</div>
        </div>
      </aside>
    </div>
  </main>;
}
