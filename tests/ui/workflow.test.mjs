import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { JSDOM } from "jsdom";
import React from "react";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost:3000" });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { render, screen, fireEvent, cleanup, act, waitFor } = await import("@testing-library/react");
const { default: Home } = await import("../../app/page.tsx");
const { default: CandidateInspector } = await import("../../app/candidate-inspector.tsx");

const fred = {
  id: "fred_unemployment", version: 2, name: "U.S. unemployment rate", description: "Seasonally adjusted U-3 percentage",
  business_context: "U.S. monthly percentage", facets: { unit: "percent", geography: "United States" },
  coverage: { start: "2018-01-01", end: "2024-12-01", rows: 84 },
  source_url: "https://fred.stlouisfed.org/series/UNRATE", source_name: "FRED", snapshot_date: "2026-09-26",
  execution_supported: true, execution_scope: "Single-product SQL",
  tables: [{ id: "unemployment_rate", columns: [{ name: "observation_date", type: "DATE", description: "Monthly date" }, { name: "unemployment_rate", type: "DOUBLE", description: "U-3", unit: "percent" }] }],
};
const sec = { ...fred, id: "sec_apple_income_statement", name: "Apple annual income statement",
  business_context: "Fiscal years, not calendar years", facets: { unit: "whole USD" } };
const products = [fred, sec];
const advice = { outcome: "recommend", limitation: "none", reason: "Assessing all catalog products",
  clarification: "", recommendations: products.map(product => ({ product, reason: "Fields and period fit", caveats: ["Review reporting basis"] })) };
const plan = { outcome: "ready", reason: "Requested period is covered", sql: "SELECT unemployment_rate FROM unemployment_rate",
  selected_fields: ["unemployment_rate"], formulas: [], assumptions: ["Seasonally adjusted"], result_units: ["percent"],
  run_id: "saved-plan", model: "fixture", planning_ms: 10, row_limit: 500 };
const result = { outcome: "answered", answer: "Returned 1 row", columns: ["rate"], rows: [{ rate: 14.8 }],
  truncated: false, execution_ms: 2, trace: [{ stage: "DuckDB execution", result: "1 row" }] };
const external = { title: "Official data API", publisher: "example.org", url: "https://example.org/data",
  evidence: "Dataset documentation", relevance: "Review suitability", provider: "exa", discovered_at: "2026-09-27",
  coverage: "Unknown", units: "Unknown", access: "Unknown", licensing: "Unknown" };
let calls = [], serverRun;
const realFetch = globalThis.fetch;
const json = (data, status = 200) => Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } }));
function state(extra = {}) {
  return { id: "11111111-1111-4111-8111-111111111111", revision: 1, status: "ready", stage: "recommendations",
    question: "What was unemployment in April 2020?", allowed_actions: ["select_source", "none_fit"],
    selected: null, confirmed: false, approved: false, advice, plan: null, result: null, external: null, error: null,
    trace: [{ stage: "advisor", status: "completed", result: "recommend", model: "fixture", duration_ms: 10, usage: { total_tokens: 12 } }], ...extra };
}
function mockFetch(overrides = {}, initialRun = null) {
  calls = []; serverRun = initialRun; window.sessionStorage.clear();
  globalThis.fetch = (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    const method = options.method ?? "GET";
    calls.push({ url, method, body });
    if (url === "/api/catalog") return overrides.catalog?.() ?? json(products);
    if (url === "/api/retrieval/search") return overrides.search?.(body) ?? json({ products: [fred] });
    if (url === "/api/workflows") {
      if (overrides.create) return overrides.create(body);
      serverRun = state({ question: body.question });
      if (body.product_id) serverRun = { ...serverRun, advice: null, selected: products.find(p => p.id === body.product_id),
        stage: "source_review", allowed_actions: ["select_source", "choose_again", "confirm_source"] };
      return json(serverRun);
    }
    if (url.endsWith("/actions")) {
      if (overrides[body.action.type]) return overrides[body.action.type](body);
      const kind = body.action.type;
      const next = { ...serverRun, revision: serverRun.revision + 1, error: null };
      if (kind === "select_source") Object.assign(next, { selected: products.find(p => p.id === body.action.product_id),
        confirmed: false, approved: false, plan: null, result: null, external: null, stage: "source_review",
        allowed_actions: ["select_source", "choose_again", "confirm_source", "none_fit"] });
      if (kind === "choose_again") Object.assign(next, { selected: null, plan: null, result: null, stage: "recommendations", allowed_actions: ["select_source", "none_fit"] });
      if (kind === "confirm_source") Object.assign(next, { confirmed: true, plan, stage: "sql_review", allowed_actions: ["select_source", "choose_again", "approve_sql"] });
      if (kind === "approve_sql") Object.assign(next, { approved: true, result, stage: "results", allowed_actions: ["select_source", "choose_again"] });
      if (kind === "none_fit") Object.assign(next, { selected: null, plan: null, stage: "external_offer", allowed_actions: ["select_source", "discover_external"] });
      if (kind === "discover_external") Object.assign(next, { external: [external], external_advice: { outcome: "recommend", answer: "Use this dataset for daily values.", primary_index: 0,
        assessments: [{ candidate_index: 0, fit: "strong", reason: "Daily observations", caveat: "Check units", evidence_quote: "Dataset documentation" }], unresolved: ["Access terms"] },
        stage: "external_review", allowed_actions: ["select_source", "discover_external", "select_external"] });
      if (kind === "select_external") Object.assign(next, { external_index: 0, external_links: [{ url: "https://example.org/data.csv", name: "data.csv" }],
        stage: "external_file_review", allowed_actions: ["select_source", "select_external", "approve_external_file"] });
      if (kind === "recover_local") Object.assign(next, { selected: null, plan: null, confirmed: false, stage: "recommendations", advice, allowed_actions: ["select_source", "none_fit"] });
      serverRun = next; return json(next);
    }
    if (url.startsWith("/api/workflows/")) {
      if (method === "DELETE") return json({ ...serverRun, status: "cancelled", selected: null, plan: null, result: null, allowed_actions: [] });
      return overrides.get?.() ?? json(serverRun);
    }
    throw new Error("Unexpected API: " + url);
  };
}
afterEach(() => { cleanup(); window.sessionStorage.clear(); globalThis.fetch = realFetch; });
async function choose(name = fred.name) {
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "What was unemployment in April 2020?" } });
  fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
  await screen.findByText("Recommended local sources — please choose");
  fireEvent.click(screen.getByRole("button", { name: "Review this dataset: " + name }));
  await screen.findByRole("checkbox", { name: /I confirm/ });
  await waitFor(() => assert.equal(screen.getByRole("checkbox", { name: /I confirm/ }).disabled, false));
}
async function generate() {
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  await waitFor(() => assert.equal(screen.getByRole("button", { name: "Generate SQL for review" }).disabled, false));
  fireEvent.click(screen.getByRole("button", { name: "Generate SQL for review" }));
  await screen.findByText("Review the query plan");
}
async function execute() {
  fireEvent.click(screen.getByRole("checkbox", { name: /I approve this SQL/ }));
  await waitFor(() => assert.equal(screen.getByRole("button", { name: "Execute approved SQL" }).disabled, false));
  fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
  await screen.findByRole("heading", { name: "Results" });
}
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

test("marketplace separates semantic search from analysis and opens product details", async () => {
  mockFetch(); render(React.createElement(Home));
  await screen.findByText("2 data products");
  assert.equal(screen.getAllByRole("searchbox").length, 1);
  assert.equal(screen.queryByText("Limited execution baseline"), null);
  assert.ok(screen.getByRole("link", { name: "Review benchmark →" }));
  assert.ok(screen.getByRole("link", { name: "Ask a question →" }));
  assert.ok(calls.every(c => c.url === "/api/catalog"));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "unemployment" } });
  fireEvent.click(screen.getByRole("button", { name: "Search datasets" }));
  await screen.findByText("1 semantic match");
  assert.deepEqual(calls.find(c => c.url === "/api/retrieval/search").body, { question: "unemployment", top_k: 10 });
  assert.equal(calls.some(c => c.url === "/api/workflows"), false);
  fireEvent.click(screen.getByRole("button", { name: fred.name }));
  assert.ok(screen.getByRole("dialog", { name: fred.name }));
  assert.ok(screen.getByRole("link", { name: "Ask about this dataset →" }).getAttribute("href").includes(fred.id));
  fireEvent.keyDown(window, { key: "Escape" });
  assert.equal(screen.queryByRole("dialog"), null);
});
test("marketplace search failure leaves the full catalog browsable", async () => {
  mockFetch({ search: () => json({ detail: "Retrieval unavailable" }, 503) });
  render(React.createElement(Home));
  await screen.findByText("2 data products");
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "labor" } });
  fireEvent.click(screen.getByRole("button", { name: "Search datasets" }));
  await screen.findByText(/Retrieval unavailable/);
  assert.equal(screen.getAllByRole("row").length, 3);
  assert.equal(calls.some(c => c.url === "/api/workflows"), false);
});
test("late semantic results cannot replace a newer marketplace query", async () => {
  const pending = deferred();
  mockFetch({ search: () => pending.promise });
  render(React.createElement(Home));
  await screen.findByText("2 data products");
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "labor" } });
  fireEvent.click(screen.getByRole("button", { name: "Search datasets" }));
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "GDP" } });
  await act(async () => pending.resolve(await json({ products: [fred] })));
  assert.equal(screen.queryByText("1 semantic match"), null);
  assert.ok(screen.getByText("2 data products"));
});

for (const source of products) {
  test(source.name + ": recommendations and selection cannot generate or execute SQL", async () => {
    mockFetch(); render(React.createElement(CandidateInspector, { products }));
    await choose(source.name);
    assert.equal(calls.filter(c => c.body?.action?.type === "confirm_source").length, 0);
    assert.equal(screen.getByText("Inspect schema and fields").closest("details").open, false);
    assert.equal(screen.getByRole("button", { name: "Generate SQL for review" }).disabled, true);
    await generate();
    assert.deepEqual(calls.find(c => c.body?.action?.type === "confirm_source").body.action, { type: "confirm_source", confirmed: true });
    assert.equal(screen.getByRole("button", { name: "Execute approved SQL" }).disabled, true);
    await execute();
    assert.deepEqual(calls.find(c => c.body?.action?.type === "approve_sql").body.action,
      { type: "approve_sql", approved: true, plan_id: "saved-plan" });
    assert.ok(screen.getByText("14.8"));
    assert.equal(screen.getAllByText(/Snapshot 2026-09-26/).length, 1);
    assert.ok(screen.getByText("Run trace"));
    assert.ok(screen.getByText("Agent workflow trace"));
    assert.ok(calls.every(c => !c.url.includes("sql-runs") && !c.url.includes("retrieval/search")));
  });
}
test("manual catalog choice bypasses advisor", async () => {
  mockFetch(); render(React.createElement(CandidateInspector, { products }));
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Apple revenue" } });
  fireEvent.click(screen.getByText("Browse all 2 local products"));
  fireEvent.click(screen.getByRole("button", { name: sec.name }));
  await screen.findByRole("checkbox", { name: /I confirm/ });
  assert.equal(calls.find(c => c.url === "/api/workflows").body.product_id, sec.id);
  assert.equal(screen.queryByText("Recommended local sources — please choose"), null);
});
test("marketplace product link can preselect a source without bypassing confirmation", async () => {
  mockFetch();
  render(React.createElement(CandidateInspector, { products, initialQuestion: "Apple revenue", initialProductId: sec.id }));
  await waitFor(() => assert.equal(screen.getByLabelText("Your question").value, "Apple revenue"));
  fireEvent.click(screen.getByRole("button", { name: `Review ${sec.name}` }));
  await screen.findByRole("checkbox", { name: /I confirm Apple/ });
  assert.equal(calls.find(c => c.url === "/api/workflows").body.product_id, sec.id);
  assert.equal(calls.some(c => c.body?.action?.type === "confirm_source"), false);
});
test("source switch immediately clears SQL and resets both approvals", async () => {
  mockFetch(); render(React.createElement(CandidateInspector, { products }));
  await choose(); await generate();
  fireEvent.click(screen.getByRole("checkbox", { name: /I approve/ }));
  fireEvent.click(screen.getByRole("button", { name: /Sources/ }));
  fireEvent.click(screen.getByRole("button", { name: "Review this dataset: " + sec.name }));
  assert.equal(screen.queryByText("Review the query plan"), null);
  await screen.findByRole("checkbox", { name: /I confirm Apple/ });
  assert.equal(screen.getByRole("checkbox", { name: /I confirm/ }).checked, false);
});
test("catalog change retires run; identical catalog preserves it", async () => {
  mockFetch(); const view = render(React.createElement(CandidateInspector, { products }));
  await choose(); await generate();
  view.rerender(React.createElement(CandidateInspector, { products }));
  assert.ok(screen.getByText("Review the query plan"));
  view.rerender(React.createElement(CandidateInspector, { products: [{ ...fred, version: 3 }, sec] }));
  assert.equal(screen.queryByText("Review the query plan"), null);
  assert.ok(calls.some(c => c.method === "DELETE"));
});
for (const phase of ["create", "confirm_source", "approve_sql"]) {
  test("late " + phase + " cannot restore state after question edit", async () => {
    const pending = deferred();
    mockFetch({ [phase]: () => pending.promise });
    render(React.createElement(CandidateInspector, { products }));
    if (phase === "create") {
      fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Old question" } });
      fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
    } else {
      await choose();
      if (phase === "approve_sql") await generate();
      fireEvent.click(screen.getByRole("checkbox", { name: phase === "approve_sql" ? /I approve/ : /I confirm/ }));
      fireEvent.click(screen.getByRole("button", { name: phase === "approve_sql" ? "Execute approved SQL" : "Generate SQL for review" }));
    }
    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "New question" } });
    await act(async () => pending.resolve(await json(state({ selected: fred, plan, result }))));
    assert.equal(screen.getByLabelText("Your question").value, "New question");
    assert.equal(screen.queryByText("Review the query plan"), null);
    assert.equal(screen.queryByText("Returned 1 row"), null);
    assert.equal(window.sessionStorage.getItem("datascout.active-workflow"), null);
  });
}
for (const stage of ["source_review", "sql_review", "results", "external_review"]) {
  test("refresh restores " + stage + " without provider calls", async () => {
    const restored = state({ stage, selected: stage === "external_review" ? null : fred,
      plan: ["sql_review", "results"].includes(stage) ? plan : null, result: stage === "results" ? result : null,
      external: stage === "external_review" ? [external] : null,
      confirmed: ["sql_review", "results"].includes(stage), approved: stage === "results",
      allowed_actions: stage === "source_review" ? ["confirm_source"] : stage === "sql_review" ? ["approve_sql"] : ["select_source"] });
    mockFetch({}, restored);
    window.sessionStorage.setItem("datascout.active-workflow", restored.id);
    render(React.createElement(CandidateInspector, { products }));
    await waitFor(() => assert.equal(screen.getByLabelText("Your question").value, restored.question));
    assert.ok(calls.every(c => c.method === "GET"));
    if (stage === "results") await screen.findByText("Returned 1 row");
    if (stage === "external_review") assert.ok(screen.getByRole("link", { name: /Official data API/ }));
  });
}
test("none fit offers discovery; only explicit click sends consent", async () => {
  mockFetch(); render(React.createElement(CandidateInspector, { products }));
  await choose();
  fireEvent.click(screen.getByRole("button", { name: /Sources/ }));
  fireEvent.click(screen.getByRole("button", { name: "None of these fit" }));
  await screen.findByRole("button", { name: "Find external sources" });
  assert.equal(calls.some(c => c.body?.action?.type === "discover_external"), false);
  fireEvent.click(screen.getByRole("button", { name: "Find external sources" }));
  await screen.findByText("Use this dataset for daily values.");
  assert.ok(screen.getAllByRole("link", { name: /Official data API/ }).length >= 1);
  assert.deepEqual(calls.find(c => c.body?.action?.type === "discover_external").body.action, { type: "discover_external", consent: true });
  assert.ok(screen.getByText(/Not yet queryable/));
  assert.equal(screen.queryByRole("button", { name: "Generate SQL for review" }), null);
});

test("external file requires exact user approval before acquisition", async () => {
  mockFetch(); render(React.createElement(CandidateInspector, { products }));
  await choose();
  fireEvent.click(screen.getByRole("button", { name: /Sources/ }));
  fireEvent.click(screen.getByRole("button", { name: "None of these fit" }));
  fireEvent.click(await screen.findByRole("button", { name: "Find external sources" }));
  fireEvent.click(await screen.findByRole("button", { name: "Analyze this source" }));
  await screen.findByRole("heading", { name: "Choose an exact CSV or ZIP file" });
  assert.equal(calls.some(c => c.body?.action?.type === "approve_external_file"), false);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Approve and download this file" })); });
  assert.deepEqual(calls.find(c => c.body?.action?.type === "approve_external_file").body.action,
    { type: "approve_external_file", approved: true, url: "https://example.org/data.csv" });
});
test("a landing page without files keeps other external candidates selectable", async () => {
  const restored = state({ stage: "external_file_review", external: [external, { ...external, title: "EPA AirData", url: "https://example.org/downloads" }],
    external_index: 0, external_links: [], allowed_actions: ["select_source", "select_external"] });
  mockFetch({}, restored); window.sessionStorage.setItem("datascout.active-workflow", restored.id);
  render(React.createElement(CandidateInspector, { products }));
  await screen.findByText(/No public CSV\/ZIP links were found/);
  const buttons = screen.getAllByRole("button", { name: "Analyze this source" });
  assert.equal(buttons.length, 2);
  assert.equal(buttons[1].disabled, false);
  fireEvent.click(buttons[1]);
  await waitFor(() => assert.equal(calls.filter(c => c.body?.action?.type === "select_external").length, 1));
});
test("late discovery cannot restore candidates after switching to local source", async () => {
  const pending = deferred();
  mockFetch({ discover_external: () => pending.promise });
  render(React.createElement(CandidateInspector, { products }));
  await choose();
  fireEvent.click(screen.getByRole("button", { name: /Sources/ }));
  fireEvent.click(screen.getByRole("button", { name: "None of these fit" }));
  await screen.findByRole("button", { name: "Find external sources" });
  fireEvent.click(screen.getByRole("button", { name: "Find external sources" }));
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "New local question" } });
  await act(async () => pending.resolve(await json(state({ stage: "external_review", external: [external] }))));
  assert.equal(screen.queryByRole("link", { name: /Official data API/ }), null);
});
for (const limitation of ["ambiguous", "data_gap", "unsupported_operation"]) {
  test(limitation + " is distinct and does not search automatically", async () => {
    const outcome = limitation === "ambiguous" ? "clarify" : "no_local_fit";
    mockFetch({ create: body => json(state({ question: body.question,
      advice: { outcome, limitation, reason: "Needs attention", clarification: "Which reporting period?", recommendations: [] },
      stage: limitation === "ambiguous" ? "clarification" : "no_local_fit",
      allowed_actions: limitation === "data_gap" ? ["select_source", "discover_external"] : ["select_source"] })) });
    render(React.createElement(CandidateInspector, { products }));
    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Some question" } });
    fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
    await screen.findByText("Needs attention");
    assert.equal(!!screen.queryByRole("button", { name: "Find external sources" }), limitation === "data_gap");
    assert.equal(calls.some(c => c.body?.action?.type === "discover_external"), false);
  });
}
test("planner abstention offers local recovery, not automatic search", async () => {
  mockFetch({ confirm_source: () => {
    serverRun = { ...serverRun, revision: 3, plan: { ...plan, outcome: "abstain", reason: "Need counts not rate", sql: "" },
      stage: "sql_abstain", allowed_actions: ["select_source", "recover_local", "none_fit"] };
    return json(serverRun);
  } });
  render(React.createElement(CandidateInspector, { products })); await choose();
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate SQL for review" }));
  await screen.findByText("Need counts not rate");
  assert.equal(calls.some(c => c.body?.action?.type === "recover_local"), false);
  fireEvent.click(screen.getByRole("button", { name: "Suggest another local dataset" }));
  await waitFor(() => assert.equal(screen.queryByText("Need counts not rate"), null));
  assert.ok(calls.some(c => c.body?.action?.type === "recover_local"));
});
for (const variant of ["empty", "truncated"]) {
  test(variant + " results remain visible", async () => {
    mockFetch({ approve_sql: () => json({ ...serverRun, revision: 4, stage: "results", approved: true,
      allowed_actions: ["select_source"], result: variant === "empty" ? { ...result, rows: [], outcome: "no_data",
        answer: "Empty results are not zero." } : { ...result, truncated: true } }) });
    render(React.createElement(CandidateInspector, { products })); await choose(); await generate(); await execute();
    assert.ok(screen.getByText(variant === "empty" ? "Empty results are not zero." : /Results truncated/));
  });
}
test("catalog and advisor failures visible; retry is explicit", async () => {
  mockFetch({ catalog: () => json({ detail: "Catalog unavailable" }, 503) });
  render(React.createElement(Home)); await screen.findByRole("alert"); cleanup();
  mockFetch({ create: () => json(state({ advice: null, stage: "error", error: "Advisor unavailable", allowed_actions: ["retry", "select_source"] })) });
  render(React.createElement(CandidateInspector, { products }));
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "GDP" } });
  fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
  await screen.findByText("Advisor unavailable");
  assert.ok(screen.getByRole("button", { name: "Retry failed step" }));
  assert.equal(calls.filter(c => c.method === "POST").length, 1);
});
test("failed execution cannot invent results", async () => {
  mockFetch({ approve_sql: () => json({ ...serverRun, revision: 4, stage: "error", error: "Execution timed out", allowed_actions: ["retry", "select_source"] }) });
  render(React.createElement(CandidateInspector, { products })); await choose(); await generate();
  fireEvent.click(screen.getByRole("checkbox", { name: /I approve/ }));
  fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
  await screen.findByText("Execution timed out");
  assert.equal(screen.queryByRole("heading", { name: "Results" }), null);
});

test("running initial workflow persists its ID before recommendations arrive", async () => {
  mockFetch({ create: body => json(state({ question: body.question, status: "running", stage: "starting", advice: null, allowed_actions: [] })) });
  render(React.createElement(CandidateInspector, { products }));
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "GDP" } });
  fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
  await waitFor(() => assert.equal(window.sessionStorage.getItem("datascout.active-workflow"), state().id));
  assert.equal(screen.getByRole("button", { name: "Working…" }).disabled, true);
  assert.equal(calls.some(c => c.body?.action), false);
});
