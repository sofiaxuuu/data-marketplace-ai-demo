"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import SiteHeader from "../site-header";
import { useCatalog } from "../use-catalog";

type Item = { id: string; title: string; updated: number; product_id: string | null; product_version: number | null };

export default function AnalyzePage() {
  return <Suspense fallback={<main className="shell"><SiteHeader active="analyze" /><p className="analysis-shell">Loading conversations…</p></main>}><AnalyzeClient /></Suspense>;
}

function AnalyzeClient() {
  const router = useRouter();
  const params = useSearchParams();
  const { products } = useCatalog();
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const started = useRef(false);
  const createKey = useRef<string | null>(null);
  const productId = params.get("product_id");
  const question = params.get("q");

  async function create(product?: { id: string; version: number }) {
    if (busy) return;
    setBusy(true); setError("");
    createKey.current ??= crypto.randomUUID();
    try {
      const response = await fetch("/api/conversations", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ request_id: createKey.current,
          ...(product ? { product_id: product.id, manifest_version: product.version } : {}) }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail ?? "Could not create conversation");
      router.replace(`/analyze/${data.conversation.id}${question ? `?q=${encodeURIComponent(question)}` : ""}`);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Could not create conversation"); setBusy(false); }
  }

  useEffect(() => {
    fetch("/api/conversations", { cache: "no-store" }).then(response => response.json()).then(data => setItems(data.conversations ?? []))
      .catch(() => setError("Could not load saved conversations."));
  }, []);
  useEffect(() => {
    if (started.current || !productId || !products.length) return;
    const product = products.find(item => item.id === productId);
    if (!product) { setError("Dataset not found in the current catalog."); return; }
    started.current = true;
    void create(product);
  }, [productId, products]);

  return <main className="shell"><SiteHeader active="analyze" /><div className="analysis-shell">
    <div className="intro"><p className="eyebrow">ASK DATASCOUT</p><h1>Conversations grounded in data.</h1>
      <p>Find a source or start from a dataset, then ask bounded analytical questions. Every SQL execution requires your approval.</p></div>
    <div className="conversation-list-heading"><h2>Saved conversations</h2><button className="primary" disabled={busy} onClick={() => void create()}>New conversation</button></div>
    {error && <p role="alert" className="notice error">{error}</p>}
    {busy && <p role="status">Opening conversation…</p>}
    {!items.length && !busy && <p>No saved conversations yet. Start one above, or open a product from the marketplace.</p>}
    <ul className="conversation-list">{items.map(item => <li key={item.id}><a href={`/analyze/${item.id}`}>
      <strong>{item.title}</strong><span>{item.product_id ? `${item.product_id} · v${item.product_version}` : "Source discovery"}</span>
      <small>Updated {new Date(item.updated * 1000).toLocaleString()}</small>
    </a></li>)}</ul>
  </div></main>;
}
