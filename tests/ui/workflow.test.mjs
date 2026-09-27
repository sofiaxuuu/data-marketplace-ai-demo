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
const { render, screen, fireEvent, cleanup, act } = await import("@testing-library/react");
const { default: Home } = await import("../../app/page.tsx");
const { default: CandidateInspector } = await import("../../app/candidate-inspector.tsx");

const fred = {
  id: "fred_unemployment", version: 2, name: "U.S. unemployment rate", description: "Seasonally adjusted U-3 percentage",
  business_context: "U.S. monthly percentage", facets: { unit: "percent", geography: "United States" },
  coverage: { start: "2018-01-01", end: "2024-12-01", rows: 84 },
  source_url: "https://fred.stlouisfed.org/series/UNRATE", source_name: "FRED", snapshot_date: "2026-09-26",
  execution_supported: true, execution_scope: "Single-product SQL; subject to supported operations and snapshot coverage",
  tables: [{ id: "unemployment_rate", columns: [{ name: "observation_date", type: "DATE", description: "Monthly date" }, { name: "unemployment_rate", type: "DOUBLE", description: "U-3", unit: "percent" }] }],
};
const sec = { ...fred, id: "sec_apple_income_statement", version: 2, name: "Apple annual income statement",
  description: "Apple fiscal annual income", business_context: "Fiscal years, not calendar years", facets: { unit: "whole USD" },
  execution_supported: true };
const products = [fred, sec];
const plan = { outcome: "ready", reason: "Requested period is covered", sql: "SELECT unemployment_rate FROM unemployment_rate",
  selected_fields: ["unemployment_rate"], formulas: [], assumptions: ["Seasonally adjusted"], result_units: ["percent"],
  run_id: "saved-plan", model: "fixture", planning_ms: 10, row_limit: 500, trace: [] };
const result = { outcome: "answered", answer: "Returned 1 row", columns: ["rate"], rows: [{ rate: 14.8 }],
  truncated: false, execution_ms: 2, trace: [{ stage: "DuckDB execution", result: "1 row" }] };
let calls = [];
const realFetch = globalThis.fetch;
const json = (data, status = 200) => Promise.resolve(new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } }));
function mockFetch(overrides = {}) {
  calls = [];
  globalThis.fetch = (url, options = {}) => {
    calls.push({ url, body: options.body ? JSON.parse(options.body) : null });
    if (overrides[url]) return overrides[url](options);
    if (url === "/api/catalog") return json(products);
    if (url === "/api/retrieval/search") return json({ products });
    if (url === "/api/sql-runs/generate") return json(plan);
    if (url === "/api/sql-runs/execute") return json(result);
    throw new Error(`Unexpected API: ${url}`);
  };
}
afterEach(() => { cleanup(); globalThis.fetch = realFetch; });

async function choose(name = fred.name) {
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "What was unemployment in April 2020?" } });
  fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
  await screen.findByText("Top candidates — please choose");
  fireEvent.click(screen.getAllByRole("button", { name })[0]);
}
async function generate() {
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate SQL for review" }));
  await screen.findByText("Review the query plan");
}
function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

test("home has one question input, updated catalog copy and no legacy baseline or sidebar trace", async () => {
  mockFetch(); render(React.createElement(Home));
  await screen.findByText("2 local products");
  assert.equal(screen.getAllByRole("textbox").length, 1);
  assert.equal(screen.queryByText("Limited execution baseline"), null);
  assert.equal(screen.queryByText("RUN TRACE"), null);
  assert.ok(screen.getByRole("link", { name: "Review benchmark →" }));
  assert.ok(screen.getByText(/All 15 products support/));
  fireEvent.click(screen.getByRole("button", { name: /Try an example/ }));
  assert.equal(screen.getByLabelText("Your question").value, "What was the U.S. unemployment rate in April 2020?");
  assert.ok(calls.every(c => c.url === "/api/catalog"));
});

for (const source of products) {
  test(`${source.name}: source and SQL approvals gate calls and results retain provenance/trace`, async () => {
    mockFetch(); render(React.createElement(CandidateInspector, { products }));
    await choose(source.name);
    assert.equal(calls.some(c => c.url.includes("sql-runs")), false);
    assert.equal(screen.getByText("Inspect schema and fields").closest("details").open, false);
    assert.equal(screen.getByRole("button", { name: "Generate SQL for review" }).disabled, true);
    await generate();
    assert.equal(calls.find(c => c.url.endsWith("generate")).body.product_id, source.id);
    assert.equal(screen.getByRole("button", { name: "Execute approved SQL" }).disabled, true);
    assert.equal(calls.some(c => c.url.endsWith("execute")), false);
    fireEvent.click(screen.getByRole("checkbox", { name: /I approve this SQL/ }));
    fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
    await screen.findByText("Returned 1 row");
    assert.deepEqual(calls.find(c => c.url.endsWith("execute")).body, { run_id: "saved-plan", approved: true });
    assert.ok(screen.getByText("14.8"));
    assert.equal(screen.getAllByText(/Snapshot 2026-09-26/).length, 2);
    assert.ok(screen.getByText("Run trace"));
    assert.ok(calls.every(c => !c.url.startsWith("/api/runs/")));
    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Different question" } });
    assert.equal(screen.queryByText("Returned 1 row"), null);
    assert.equal(screen.queryByRole("checkbox", { name: /I approve/ }), null);
  });
}

test("switching source clears generated SQL and both approvals", async () => {
  mockFetch(); render(React.createElement(CandidateInspector, { products }));
  await choose(); await generate();
  fireEvent.click(screen.getByRole("checkbox", { name: /I approve/ }));
  fireEvent.click(screen.getAllByRole("button", { name: sec.name })[0]);
  assert.equal(screen.queryByText("Review the query plan"), null);
  assert.equal(screen.getByRole("checkbox", { name: /I confirm/ }).checked, false);
  assert.equal(screen.getByRole("button", { name: "Generate SQL for review" }).disabled, true);
});

test("catalog version change clears downstream state; identical catalog does not", async () => {
  mockFetch(); const view = render(React.createElement(CandidateInspector, { products }));
  await choose(); await generate();
  view.rerender(React.createElement(CandidateInspector, { products }));
  assert.ok(screen.getByText("Review the query plan"));
  view.rerender(React.createElement(CandidateInspector, { products: [{ ...fred, version: 3 }, sec] }));
  assert.equal(screen.queryByText("Review the query plan"), null);
  assert.equal(screen.queryByRole("checkbox", { name: /I confirm/ }), null);
});

for (const phase of ["search", "generate", "execute"]) {
  test(`late ${phase} response cannot restore old state after the question changes`, async () => {
    const pending = deferred();
    mockFetch({ [`/api/${phase === "search" ? "retrieval/search" : `sql-runs/${phase}`}`]: () => pending.promise });
    render(React.createElement(CandidateInspector, { products }));
    if (phase === "search") {
      fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Old question" } });
      fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
    } else {
      await choose();
      fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
      fireEvent.click(screen.getByRole("button", { name: "Generate SQL for review" }));
      if (phase === "execute") {
        await screen.findByText("Review the query plan");
        fireEvent.click(screen.getByRole("checkbox", { name: /I approve/ }));
        fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
      }
    }
    fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "New question" } });
    await act(async () => { pending.resolve(await json(phase === "search" ? { products } : phase === "generate" ? plan : result)); });
    assert.equal(screen.queryByText("Top candidates — please choose"), null);
    assert.equal(screen.queryByText("Review the query plan"), null);
    assert.equal(screen.queryByText("Returned 1 row"), null);
  });
}

for (const outcome of ["clarify", "abstain"]) {
  test(`${outcome} stays visible without an execution button`, async () => {
    mockFetch({ "/api/sql-runs/generate": () => json({ ...plan, outcome, reason: "Reporting period needs attention", sql: "" }) });
    render(React.createElement(CandidateInspector, { products })); await choose();
    fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
    fireEvent.click(screen.getByRole("button", { name: "Generate SQL for review" }));
    await screen.findByText("Reporting period needs attention");
    assert.equal(screen.queryByRole("button", { name: "Execute approved SQL" }), null);
  });
}

for (const variant of ["empty", "truncated"]) {
  test(`${variant} results remain explicit`, async () => {
    mockFetch({ "/api/sql-runs/execute": () => json(variant === "empty" ? { ...result, outcome: "no_data", rows: [], answer: "No matching observations. Empty results are not zero." } : { ...result, truncated: true }) });
    render(React.createElement(CandidateInspector, { products })); await choose(); await generate();
    fireEvent.click(screen.getByRole("checkbox", { name: /I approve/ }));
    fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
    await screen.findByRole("heading", { name: "Results" });
    assert.ok(screen.getByText(variant === "empty" ? /Empty results are not zero/ : /Results truncated/));
  });
}

test("catalog, retrieval and SQL failures are visible", async () => {
  mockFetch({ "/api/catalog": () => json({ detail: "Unavailable" }, 503) });
  render(React.createElement(Home)); await screen.findByRole("alert"); cleanup();
  mockFetch({ "/api/retrieval/search": () => json({ detail: "Retrieval unavailable" }, 503) });
  render(React.createElement(CandidateInspector, { products }));
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "GDP 2024" } });
  fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
  await screen.findByText("Retrieval unavailable"); cleanup();
  mockFetch({ "/api/sql-runs/generate": () => json({ detail: "Planner unavailable" }, 503) });
  render(React.createElement(CandidateInspector, { products })); await choose();
  fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
  fireEvent.click(screen.getByRole("button", { name: "Generate SQL for review" }));
  await screen.findByText("Planner unavailable");
});

test("empty retrieval does not claim no suitable source and leaves catalog browsing available", async () => {
  mockFetch({ "/api/retrieval/search": () => json({ products: [] }) });
  render(React.createElement(CandidateInspector, { products }));
  fireEvent.change(screen.getByLabelText("Your question"), { target: { value: "Unclear measure" } });
  fireEvent.click(screen.getByRole("button", { name: "Find candidates" }));
  await screen.findByText(/not proof that no source fits/);
  assert.ok(screen.getByText("Browse all 2 local products"));
  assert.equal(calls.some(c => c.url.includes("sql-runs")), false);
});

test("execution failure is visible without displaying invented results", async () => {
  mockFetch({ "/api/sql-runs/execute": () => json({ detail: "Query exceeded the execution timeout" }, 422) });
  render(React.createElement(CandidateInspector, { products })); await choose(); await generate();
  fireEvent.click(screen.getByRole("checkbox", { name: /I approve/ }));
  fireEvent.click(screen.getByRole("button", { name: "Execute approved SQL" }));
  await screen.findByText("Query exceeded the execution timeout");
  assert.equal(screen.queryByRole("heading", { name: "Results" }), null);
  assert.ok(screen.getByText("Review the query plan"));
});
