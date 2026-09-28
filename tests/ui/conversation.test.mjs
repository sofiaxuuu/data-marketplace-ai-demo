import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { JSDOM } from "jsdom";
import React from "react";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime.js";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost:3000/analyze/example" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { render, screen, fireEvent, cleanup, waitFor } = await import("@testing-library/react");
const { default: ConversationChat } = await import("../../app/conversation-chat.tsx");

const product = { id: "fred_unemployment", version: 2, name: "U.S. unemployment rate", description: "Monthly rate",
  business_context: "U.S. U-3 percentage", facets: { unit: "percent" },
  coverage: { start: "2018-01-01", end: "2024-12-01", rows: 84 }, source_url: "https://fred.stlouisfed.org/series/UNRATE",
  source_name: "FRED", snapshot_date: "2026-09-26", execution_supported: true, execution_scope: "Single table",
  tables: [{ id: "unemployment_rate", columns: [{ name: "observation_date", type: "DATE", description: "Date" },
    { name: "unemployment_rate", type: "DOUBLE", description: "Rate", unit: "percent" }] }] };
const plan = { outcome: "ready", reason: "Covered", sql: "SELECT unemployment_rate FROM unemployment_rate LIMIT 1",
  selected_fields: ["unemployment_rate"], formulas: [], assumptions: [], result_units: ["percent"],
  run_id: "plan-1", model: "fixture", planning_ms: 1, row_limit: 500 };
const result = { outcome: "answered", answer: "Rate: 4.1 percent", columns: ["rate"], rows: [{ rate: 4.1 }],
  truncated: false, execution_ms: 1, trace: [] };
const conversation = { id: "11111111-1111-4111-8111-111111111111", title: product.name, revision: 2,
  product_id: product.id, product_version: product.version, active_run_id: "22222222-2222-4222-8222-222222222222" };
const run = { id: conversation.active_run_id, revision: 1, status: "ready", stage: "source_review",
  question: "What was unemployment in April 2020?", analysis_question: "What was unemployment in April 2020?",
  interpreted_question: "What was unemployment in April 2020?", turn_index: 1, intent: "analysis",
  selected: product, confirmed: false, approved: false, plan: null, result: null, advice: null, external: null,
  error: null, trace: [],
  allowed_actions: ["confirm_source"] };
let state, calls;
const originalFetch = globalThis.fetch;
const response = value => Promise.resolve(new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } }));
function install(initial = run) {
  state = { conversation: { ...conversation }, events: [], run: { ...initial } }; calls = [];
  globalThis.fetch = (url, options = {}) => {
    const method = options.method ?? "GET";
    const body = options.body && typeof options.body === "string" ? JSON.parse(options.body) : null;
    calls.push({ url, method, body });
    if (url === `/api/conversations/${conversation.id}` && method === "GET") return response(state);
    if (url === `/api/conversations/${conversation.id}/turns`) {
      state = { ...state, conversation: { ...state.conversation, revision: state.conversation.revision + 1 },
        run: { ...state.run, revision: state.run.revision + 1, stage: "question_review", analysis_question: body.question,
          interpreted_question: "What was the unemployment rate in July 2020?", plan: null, result: null,
          approved: false, allowed_actions: ["confirm_interpretation", "ask_question"] } };
      return response(state);
    }
    if (url.endsWith("/actions")) {
      const kind = body.action.type;
      if (kind === "confirm_source" || kind === "confirm_interpretation") state = { ...state, run: { ...state.run,
        revision: state.run.revision + 1, stage: "sql_review", confirmed: true, plan, allowed_actions: ["approve_sql"] } };
      if (kind === "approve_sql") state = { ...state, run: { ...state.run, revision: state.run.revision + 1,
        stage: "results", approved: true, result, allowed_actions: ["ask_question"] } };
      return response(state.run);
    }
    throw new Error(`Unexpected API: ${url}`);
  };
}
function renderChat() {
  const router = { push() {}, replace() {}, back() {}, forward() {}, refresh() {}, prefetch() {} };
  return render(React.createElement(AppRouterContext.Provider, { value: router },
    React.createElement(ConversationChat, { id: conversation.id, products: [product] })));
}
afterEach(() => { cleanup(); globalThis.fetch = originalFetch; });

test("dataset chat requires confirmation and fresh SQL approval for each turn", async () => {
  install(); renderChat();
  await screen.findByText("Review this dataset");
  assert.equal(calls.filter(call => call.method === "POST").length, 0);
  assert.ok(screen.getByRole("button", { name: "Confirm dataset" }).disabled);
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm this dataset/ }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm dataset" }));
  await screen.findByText("Review the query plan");
  assert.equal(calls.filter(call => call.body?.action?.type === "approve_sql").length, 0);
  fireEvent.click(screen.getByRole("checkbox", { name: /I approve this SQL/ }));
  fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
  await screen.findByText("Rate: 4.1 percent");
  fireEvent.change(screen.getByLabelText("Ask an analytical question about this dataset"), { target: { value: "What about July?" } });
  fireEvent.click(screen.getByRole("button", { name: "Send question" }));
  await screen.findByText("What was the unemployment rate in July 2020?");
  assert.equal(calls.filter(call => call.body?.action?.type === "approve_sql").length, 1);
  fireEvent.click(screen.getByRole("button", { name: "Generate SQL for this interpretation" }));
  await waitFor(() => assert.equal(screen.getAllByText("Review the query plan").length, 1));
  assert.equal(calls.filter(call => call.body?.action?.type === "approve_sql").length, 1);
});

test("source-finding offers discovery without upload or premature SQL", async () => {
  install({ ...run, selected: null, confirmed: false, analysis_question: null, intent: "source_finding",
    stage: "no_local_fit", advice: { outcome: "no_local_fit", reason: "No local coverage", clarification: "", recommendations: [] },
    allowed_actions: ["discover_external"] });
  renderChat();
  await screen.findByText("Look beyond the catalog");
  assert.equal(calls.filter(call => call.method === "POST").length, 0);
  assert.equal(screen.queryByText("Review the query plan"), null);
  assert.ok(screen.getByRole("button", { name: "Find external sources" }));
  assert.equal(screen.queryByRole("button", { name: "Validate this file" }), null);
});
