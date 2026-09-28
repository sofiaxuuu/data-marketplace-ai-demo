"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { InspectProduct } from "./candidate-inspector";

export type Plan = {
  outcome: "ready" | "clarify" | "abstain"; reason: string; sql: string;
  selected_fields: string[]; formulas: string[]; assumptions: string[]; result_units: string[];
  run_id?: string; model: string; planning_ms?: number; row_limit?: number;
};
export type Result = { outcome: "answered" | "no_data"; answer: string; columns: string[];
  rows: Record<string, string | number | boolean | null>[]; truncated: boolean; execution_ms: number;
  trace: { stage: string; result: string }[] };
export type Workflow = {
  id: string; revision: number; status: string; stage: string; question: string; allowed_actions: string[];
  conversation_id?: string | null; analysis_question?: string | null; interpreted_question?: string | null;
  interpretation_clarification?: string | null; turn_index?: number; intent?: "source_finding" | "analysis";
  selected: InspectProduct | null; confirmed: boolean; approved: boolean; plan: Plan | null; result: Result | null;
  advice: { outcome: "recommend" | "clarify" | "no_local_fit"; limitation: string; reason: string; clarification: string;
    recommendations: { product: InspectProduct; reason: string; caveats: string[] }[] } | null;
  external: { title: string; publisher: string; url: string; evidence: string; relevance: string; provider: string;
    discovered_at: string; coverage: string; units: string; access: string; licensing: string }[] | null;
  external_advice: { outcome: "recommend" | "insufficient_evidence"; answer: string; primary_index: number | null;
    assessments: { candidate_index: number; fit: "strong" | "partial" | "poor"; reason: string; caveat: string; evidence_quote: string }[]; unresolved: string[] } | null;
  external_links: { url: string; name: string }[] | null;
  external_index: number | null;
  external_file: { url: string; origin?: "download" | "upload"; filename?: string; source_page?: string | null;
    retrieved_at: string; sha256: string; rows: number; start: string; end: string;
    raw_sha256: string; units_observed: string[]; sample_rows: Record<string, string | null>[];
    columns: { name: string; type: string; description: string }[] } | null;
  registration: { id: string; name: string; description: string; source_url: string; adapter: string;
    coverage: { start: string; end: string; rows: number }; columns: { name: string; type: string; description: string }[];
    product_id?: string; status?: string } | null;
  error: string | null; trace: { stage: string; status: string; result: string; duration_ms: number;
    model?: string; provider?: string; usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number } }[];
};

const STORAGE_KEY = "datascout.active-workflow";
function savedId() { try { return window.sessionStorage.getItem(STORAGE_KEY); } catch { return null; } }
function saveId(id: string | null) {
  try { if (id) window.sessionStorage.setItem(STORAGE_KEY, id); else window.sessionStorage.removeItem(STORAGE_KEY); } catch { /* storage-disabled browser */ }
}

export function useWorkflow(products: InspectProduct[], initialQuestion = "") {
  const [question, setQuestion] = useState("");
  const [run, setRun] = useState<Workflow | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const runRef = useRef<Workflow | null>(null);
  const catalogRef = useRef<string | null>(null);
  const mounted = useRef(false);
  const requesting = useRef(false);

  const accept = useCallback((value: Workflow) => {
    const current = runRef.current;
    if (current?.id === value.id && value.revision < current.revision) return;
    if (current?.id === value.id && value.revision === current.revision && current.status !== "running" && value.status === "running") return;
    runRef.current = value; setRun(value); setQuestion(value.question); saveId(value.id);
  }, []);

  const retire = useCallback(() => {
    generation.current += 1;
    const id = runRef.current?.id ?? savedId();
    requesting.current = false;
    runRef.current = null; setRun(null); setBusy(false); setError(""); saveId(null);
    if (id) void fetch(`/api/workflows/${id}`, { method: "DELETE" }).catch(() => {});
  }, []);

  useEffect(() => {
    mounted.current = true;
    const id = savedId(), current = ++generation.current;
    if (id) {
      setBusy(true);
      fetch(`/api/workflows/${id}`, { cache: "no-store", signal: AbortSignal.timeout(15000) })
        .then(async response => {
          const value = await response.json();
          if (!response.ok) {
            if (response.status === 404 || response.status === 410) saveId(null);
            throw new Error(value.detail ?? "Could not restore the workflow");
          }
          if (mounted.current && generation.current === current) accept(value);
        }).catch(e => {
          if (mounted.current && generation.current === current) setError(e.message);
        }).finally(() => { if (mounted.current && generation.current === current) setBusy(false); });
    }
    return () => { mounted.current = false; generation.current += 1; };
  }, [accept]);

  useEffect(() => {
    if (initialQuestion && !savedId() && !runRef.current) {
      setQuestion(previous => previous || initialQuestion);
    }
  }, [initialQuestion]);

  useEffect(() => {
    if (!products.length && catalogRef.current === null) return;
    const signature = JSON.stringify(products);
    if (catalogRef.current !== null && catalogRef.current !== signature) retire();
    catalogRef.current = signature;
  }, [products, retire]);

  const refresh = useCallback(async () => {
    const current = generation.current, id = runRef.current?.id ?? savedId();
    if (!id) return;
    try {
      const response = await fetch(`/api/workflows/${id}`, { cache: "no-store", signal: AbortSignal.timeout(15000) });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail ?? "Could not refresh workflow");
      if (mounted.current && current === generation.current) accept(value);
    } catch (e) { if (mounted.current && current === generation.current) setError(e instanceof Error ? e.message : "Workflow unavailable"); }
  }, [accept]);

  useEffect(() => {
    if (run?.status !== "running" || busy) return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [run?.status, busy, refresh]);

  async function request(body: object, creating: boolean) {
    if (requesting.current) return;
    requesting.current = true;
    const current = ++generation.current;
    setBusy(true); setError("");
    const id = runRef.current?.id;
    try {
      const response = await fetch(creating ? "/api/workflows" : `/api/workflows/${id}/actions`, {
        method: "POST", headers: { "content-type": "application/json" },
        signal: AbortSignal.timeout(190000), body: JSON.stringify(body),
      });
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail ?? "Workflow request failed");
      if (mounted.current && generation.current === current) accept(value);
      else if (creating && value.id) void fetch(`/api/workflows/${value.id}`, { method: "DELETE" }).catch(() => {});
    } catch (e) {
      if (mounted.current && generation.current === current) {
        setError(e instanceof Error ? e.message : "Workflow unavailable");
        if (!creating) void refresh(); // read state; never automatically retry an action
      }
    } finally { if (mounted.current && generation.current === current) { requesting.current = false; setBusy(false); } }
  }

  function start(source?: InspectProduct) {
    if (requesting.current) return Promise.resolve();
    retire();
    return request({ request_id: crypto.randomUUID(), question: question.trim(),
      ...(source ? { product_id: source.id, manifest_version: source.version } : {}) }, true);
  }
  function act(action: object) {
    if (!runRef.current || busy || requesting.current) return Promise.resolve();
    const previous = runRef.current;
    const kind = (action as { type: string }).type;
    if (["select_source", "choose_again", "recover_local", "none_fit"].includes(kind)) {
      setRun({ ...previous, selected: null, plan: null, result: null, confirmed: false, approved: false, external: null });
    }
    return request({ request_id: crypto.randomUUID(), expected_revision: runRef.current.revision, action }, false);
  }
  function changeQuestion(value: string) { retire(); setQuestion(value); }
  function select(source: InspectProduct) {
    return runRef.current ? act({ type: "select_source", product_id: source.id, manifest_version: source.version }) : start(source);
  }
  return { question, changeQuestion, run, busy: busy || run?.status === "running", error, start, act, select, refresh, retire };
}
