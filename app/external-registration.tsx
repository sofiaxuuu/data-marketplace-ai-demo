"use client";

import { useState } from "react";
import type { Workflow } from "./use-workflow";

export default function ExternalRegistration({ run, busy, onAction }: { run: Workflow; busy: boolean; onAction: (action: object) => void }) {
  const draft = run.registration;
  const [name, setName] = useState(draft?.name ?? "");
  const [description, setDescription] = useState(draft?.description ?? "");
  const [geography, setGeography] = useState("");
  const [measure, setMeasure] = useState("");
  const [unit, setUnit] = useState("");
  const [measureColumn, setMeasureColumn] = useState("");
  const [coverageColumn, setCoverageColumn] = useState("");
  const [keys, setKeys] = useState<string[]>([]);
  const [rightsReviewed, setRightsReviewed] = useState(false);
  if (!draft) return null;
  if (draft.status === "registered") return <section className="external-discovery"><h3>Added to local catalog</h3><p>{draft.product_id} is now a versioned product. Refresh the page to browse it. Metadata indexing was not requested.</p></section>;
  const columns = draft.columns;
  const canSubmit = !!name.trim() && description.trim().length >= 10 && !!geography.trim() && !!measure.trim() && !!unit.trim() && !!measureColumn && !!coverageColumn && keys.length > 0 && rightsReviewed;
  return <section className="external-discovery" aria-label="Review catalog registration">
    <h3>Add to catalog for repeatable use</h3>
    <p>This is a separate publication step. Review the source, schema mapping, coverage, units and key. No new download or SingleStore indexing occurs now.</p>
    <p>Proposed ID: <code>{draft.id}</code> · Adapter: {draft.adapter} · Coverage: {draft.coverage.start}–{draft.coverage.end} ({draft.coverage.rows} rows)</p>
    <p>Refresh recipe: public {draft.adapter.toUpperCase()} from <a href={draft.source_url} target="_blank" rel="noreferrer">{draft.source_url}</a>; future refresh requires a separate explicit command.</p>
    <div className="registration-fields">
      <label>Product name<input value={name} onChange={e => setName(e.target.value)} /></label>
      <label>Description and reporting basis<textarea value={description} onChange={e => setDescription(e.target.value)} /></label>
      <label>Geography<input value={geography} onChange={e => setGeography(e.target.value)} placeholder="For example, United States (monitor-level rows)" /></label>
      <label>Measure<input value={measure} onChange={e => setMeasure(e.target.value)} placeholder="For example, daily PM2.5 concentration" /></label>
      <label>Unit<input value={unit} onChange={e => setUnit(e.target.value)} placeholder="Use the source's exact unit" /></label>
      <label>Measure column<select value={measureColumn} onChange={e => setMeasureColumn(e.target.value)}><option value="">Choose a column</option>{columns.map(c => <option key={c.name} value={c.name}>{c.name} ({c.type})</option>)}</select></label>
      <label>Coverage column<select value={coverageColumn} onChange={e => setCoverageColumn(e.target.value)}><option value="">Choose a column</option>{columns.map(c => <option key={c.name} value={c.name}>{c.name} ({c.type})</option>)}</select></label>
      <fieldset><legend>Columns forming a unique observation key</legend>{columns.map(c => <label key={c.name}><input type="checkbox" checked={keys.includes(c.name)} onChange={e => setKeys(e.target.checked ? [...keys, c.name] : keys.filter(k => k !== c.name))} /> {c.name} — {c.description}</label>)}</fieldset>
    </div>
    <label><input type="checkbox" checked={rightsReviewed} onChange={e => setRightsReviewed(e.target.checked)} /> I reviewed the source’s access and reuse terms. DataScout has not verified licensing.</label>
    <div><button type="button" className="primary" disabled={busy || !canSubmit || !run.allowed_actions.includes("register_product")} onClick={() => onAction({ type: "register_product", approved: true, rights_reviewed: true, name, description, geography, measure, unit, measure_column: measureColumn, coverage_column: coverageColumn, unique_key: keys })}>Approve catalog registration</button></div>
  </section>;
}
