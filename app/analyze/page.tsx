"use client";

import { useEffect, useState } from "react";
import CandidateInspector from "../candidate-inspector";
import SiteHeader from "../site-header";
import { useCatalog } from "../use-catalog";

export default function AnalyzePage() {
  const { products, error } = useCatalog();
  const [initial, setInitial] = useState({ question: "", productId: "" });
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setInitial({ question: params.get("q") ?? "", productId: params.get("product_id") ?? "" });
  }, []);
  return <main className="shell"><SiteHeader active="analyze" />
    <div className="analysis-shell"><div className="intro"><p className="eyebrow">ASK DATASCOUT</p><h1>From question to evidence-backed answer.</h1>
      <p>Compare sources, confirm the dataset, review the proposed SQL, then approve execution. Each decision stays in your hands.</p></div>
      {error && <p role="alert" className="notice error">{error}</p>}
      <CandidateInspector products={products} initialQuestion={initial.question} initialProductId={initial.productId} />
    </div>
  </main>;
}
