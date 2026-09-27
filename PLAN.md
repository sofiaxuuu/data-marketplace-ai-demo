# DataScout Build Plan

## Goal

Build an open-source multi-agent data analyst that discovers the right data before generating SQL.

V1 chooses exactly one data product per question. If no single product can answer
the question, it abstains and shows the closest candidates. Cross-product joins
are out of scope for V1.

The project should demonstrate:

- semantic retrieval over data-product metadata

- dataset selection with LLMs

- human validation before query generation

- schema/column selection

- SQL generation and execution

- evaluation across retrieval quality, task success, latency, and cost

- optional external data-source discovery with Exa

## Core Product Thesis

Most data agents assume the correct database or table is already known.

DataScout should instead answer:

> How does an agent figure out which data source, dataset, and fields it should use before querying?

## Tech Stack

### Product / App

- TypeScript and Next.js for the web UI

- Next.js API routes provide the browser-facing API and proxy backend requests

### Agent + API Layer

- Python and FastAPI for retrieval, data access, and agent orchestration

- TypeScript remains responsible for the web experience

- LLM provider abstraction

- Exa Search API for external source discovery

### Evaluation / Data Work

- Python

- pandas

- pytest

- notebook or scripts for benchmark analysis

### Retrieval

- SingleStore vector database

### Embeddings

#### V1

- OpenAI `text-embedding-3-small`, 1,536 dimensions

- Store vectors in SingleStore

- Exact cosine retrieval over normalized vectors for the initial small catalog

Current implementation: FRED and World Bank metadata is embedded and stored in
`datascout_products_v1`. Ingestion skips unchanged metadata, and the UI shows
ranked candidates before deterministic selection and human confirmation. SEC
question execution and the retrieval benchmark remain pending. SEC acquisition now has
an EdgarTools recipe pinned to Apple's FY2024 10-K (accession
0000320193-24-000123), with annual consolidated USD facts, fiscal-period checks,
local Parquet and replayable extracted facts. The live download succeeded and
all 12 extracted financial values match the filing's consolidated statement.
The SEC manifest is registered; the deterministic question-execution adapter
remains to be implemented.

#### V2

- Qwen3-Embedding hosted on AWS SageMaker

- Keep SingleStore as vector database

- Benchmark hosted vs self-hosted embeddings

### Query Execution

- DuckDB over local Parquet snapshots for V1

- No live source API calls at question time

### Infrastructure

- AWS for deployment and later SageMaker hosting

## Architecture

```text

User Question

↓

Next.js UI → Next.js API proxy

↓

Python FastAPI service

↓

Data Product Retrieval

↓

SingleStore Vector Search

↓

Dataset Selection Agent

↓

Human Validation

↓

Schema / Field Selection Agent

↓

SQL Generation

↓

DuckDB Execution

↓

Answer

```

Fallback path:

```text

Catalog has no suitable single product

↓

Exa Search

↓

Discover authoritative external dataset / API / docs

↓

Extract metadata

↓

Present candidate data source for approval

↓

Register metadata after approval; resume queries only when a local Parquet
snapshot and manifest mapping are available

```

## Repository Structure

```text

datascout/

├── app/ # Next.js UI and browser-facing API proxy

├── lib/ # TypeScript UI/shared types

├── python/datascout/ # FastAPI, retrieval, ingestion, SingleStore access

├── data/

│ ├── manifests/

│ └── sample_datasets/

├── tests/ # Frontend/API contract tests

├── evals/ # Python benchmark datasets and analysis (planned)

├── infra/

│ └── aws/

├── docs/

└── README.md

```

## Data Model

Create public `dataset_manifest.yaml` files. A manifest is the source of truth for
one data product. SingleStore stores its searchable metadata, embedding, stable
product ID, and manifest version. DuckDB reads the corresponding local Parquet
snapshot; sample rows are not stored in SingleStore.

Example:

```yaml
id: sec_company_fundamentals
version: 1
name: SEC Company Fundamentals
description: >
  Quarterly and annual company financial statement facts,
  including revenue, gross profit, operating income,
  assets, liabilities, and related metrics.
tables:
  - id: company_facts
    path: data/sample_datasets/sec_company_fundamentals/company_facts.parquet
    columns:
      - name: revenue
        type: DOUBLE
        unit: USD
        description: Reported revenue for the stated fiscal period
concepts:
  - revenue
  - gross profit
  - operating income
  - assets
  - liabilities
source:
  name: SEC
  url: https://www.sec.gov/
  retrieved_at: YYYY-MM-DD
  snapshot_version: example-version
  license_or_terms: example-reference
```

Each manifest should include:

- data product name

- description

- datasets/tables

- business context

- concepts

- schema metadata

- table relationships where relevant

- source/provenance

- stable product and table IDs, plus manifest version

- local Parquet paths and a reproducible snapshot recipe

- column names, types, units, meanings, keys, and time coverage

- freshness date and source terms/attribution

The loader validates each manifest, verifies its referenced Parquet files and
columns, and registers only valid products as queryable. Re-ingestion updates a
product/version without creating duplicates. Record the embedding model and
version alongside each vector so a model change can trigger re-embedding.

## Initial Dataset Scope

Start with one small, real local Parquet snapshot for each of three public domains:

- SEC company financials

- FRED macroeconomic data

- World Bank indicators

Use a shared configuration-driven acquisition pipeline to create the snapshots.
The three initial datasets are examples, not a source/provider allowlist.
Record source URLs, extraction steps, dates, and checksums so each
snapshot can be reproduced. Do not call source APIs during user queries. Show
the snapshot date with answers so they are not mistaken for current data.
Synthetic fixtures may be used in tests, but are not a fourth V1 product.
Do not expand scope until end-to-end flow works.

## Reusable Acquisition and Future Discovery

Implemented baseline: reviewed YAML recipes configure generic CSV and REST/JSON
adapters, plus an EdgarTools-backed XBRL reader. The shared pipeline performs
field mapping, type conversion, explicit scaling, filtering, identity checks,
key/coverage validation, Parquet writing, raw-source retention and provenance.
Dataset IDs, endpoints, credentials references and schemas are configuration,
not hardcoded Python branches. Add datasets using supported formats without
new pipeline code. XBRL still requires reviewed fiscal periods/concept mappings.

Transport and interpretation are distinct. Generic HTTP acquisition currently
supports public HTTPS GET, headers from environment variables and none/page-count
pagination. CSV supports reviewed delimiters/encoding; JSON supports nested
record/field paths. Unsupported authentication, pagination or semantic
transformations require an adapter extension, not executable YAML.

CLI flow: inspect recipe → explicitly download or replay saved bytes → normalize
and validate → immutable snapshot → optionally publish manifest → optionally
index metadata in SingleStore. Network acquisition, publication and indexing
are separate explicit choices. Preserve original retrieval dates on replay.
Content/recipe hashes identify snapshots; retain old snapshots. Serialize builds
per dataset and atomically replace the manifest only after catalog validation.
Changed registrations increment manifest versions, invalidating old confirmation
and index entries; index retries do not repeat acquisition.

Old dataset scripts are compatibility wrappers. Existing active manifests and
sample Parquet files remain valid without forced migration. Catalog registration
does not imply the deterministic question selector/SQL execution supports a new
product; generalizing those components remains separate work.

External discovery, including Exa, produces candidates, not immediately queryable
tables. Lifecycle: discovered → recommended → user-selected → approved temporary
CSV/ZIP → one-off answer → optionally reviewed/registered → optionally indexed.
Record discovery provider, URL, time and evidence; inspect source
access/terms; select an existing adapter or report unsupported capabilities.
New providers are allowed through reviewed configuration. Default policy is
explicit acquisition approval and no live source calls during questions.
LLM-generated recipes are proposals requiring review. Do not execute downloaded
instructions, arbitrary SQL or Python from recipes. Reject private-network
HTTP destinations; review endpoint hosts and keep credentials out of provenance.

Future improvements: API acquisition with cursor pagination or authentication
when required, stronger metadata mappings, SQL-source adapters and scheduling.
Automatic live acquisition is not implemented; every external file requires a
user-selected source and exact-URL approval.

## V1 Query Contract

1. Retrieve ranked products from SingleStore using the question.
2. Select at most one product. Abstain when no product has the necessary concepts,
   fields, or coverage; show the closest candidates and missing requirement.
   A similarity score alone is not calibrated confidence.
3. Show the proposed product, tables, coverage, and reason for selection. The
   user can confirm it, choose another candidate, or stop. Confirmation binds
   the run to a product ID and manifest version.
4. Select fields from the confirmed product, generate visible DuckDB SQL, and
   execute against only that product's registered local Parquet tables. Reject
   SQL referencing another product or unavailable fields.
5. Return SQL, result, source, and snapshot date, or a clear failure/abstention.
   A question requiring multiple products is an abstention in V1.

Execution is read-only, restricted to registered tables and columns, and bounded
by a timeout and result-row limit. Validate SQL before execution and enforce the
same restrictions in the DuckDB connection. Do not expose secrets or local paths
in browser-visible errors or traces.

## First Vertical Slice

Before adding embeddings or SingleStore, make one question run end-to-end with a
fixed manifest and local Parquet snapshot: question → proposed product → user
confirmation → selected fields → visible SQL → DuckDB result → answer and trace.
Then add the other two product snapshots, SingleStore retrieval, and measured
selection. Keep the same manifest IDs and query contract throughout.

## Milestone 1 — Minimal Retrieval (after first vertical slice)

Build:

- manifest loader

- embedding generation

- SingleStore ingestion

- semantic search

- top-k data-product retrieval

Output:

- query

- retrieved products

- similarity scores

Acceptance:

- one command can ingest manifests

- one API route can return top-3 products

- retrieval results are logged

## Implemented expansion — Competing products and human choice

This milestone supersedes the earlier three-product evaluation assumption.
The active catalog now has 15 real products, acquired through reviewed recipes:

| Family | Products | Active coverage |
|---|---|---|
| FRED | U-3 seasonally adjusted (`UNRATE`), U-3 unadjusted (`UNRATENSA`), unemployment count (`UNEMPLOY`), broader U-6 (`U6RATE`) | Monthly 2018–2024 |
| World Bank | U.S. GDP per capita current USD, total GDP current USD, GDP per capita constant 2015 USD, GDP per capita PPP current international dollars; Canada GDP per capita current USD | Annual **2015–2024** |
| Apple SEC | Annual income, annual cash flow, balance sheet, comparative Q3 income | Annual flows FY2022–2024; fiscal-end balances FY2023–2024; Q3 FY2023/FY2024 only |
| Microsoft SEC | Annual income, annual cash flow | FY2022–2024 |

The original U.S. GDP snapshot is retained, but its active manifest now points
to a versioned 2015–2024 snapshot. Existing product IDs are preserved. All
products expose reviewed comparison facets; the same metadata is embedded in
SingleStore using the existing model. SEC duration/instant facts use exact fiscal
contexts, not inferred calendar periods; quarterly extraction excludes YTD.

The browser now has a separate candidate inspector: retrieve top five, compare
definitions/units/coverage, explicitly choose a product/version and inspect its
schema and source. No automatic selection or SQL runs on inspection. Clarification
means human choice, not "no suitable data." Catalog refreshes and question changes
clear stale choices. Retrieval/API metadata excludes local paths and credentials.
The original deterministic backend supports only adjusted U-3 and current-USD
U.S. GDP-per-capita templates. Its endpoints and regression tests are retained
for compatibility; its separate home-page UI has been removed.

The expanded benchmark has 60 cases: 30 answerable, 15 human clarification and
15 unavailable/multi-source. Development and held-out each have 30 cases and one
answerable question per product. Product versions, metadata and snapshot hashes
are frozen; schema/period evidence is mechanically verified. Semantic labels
remain agent-authored, pending human review. This is not official FinSearchComp.

Measure Recall@1/3/5, full-catalog MRR, per-product/split results, clarification
candidate coverage and latency. Do **not** claim automatic abstention, selection
or numeric-answer accuracy from this retrieval-only benchmark. Preserve the old
36-case benchmark as an archived three-product local smoke test.

Initial untuned live baseline: Recall@1 80%, Recall@3 96.7%, Recall@5 100%,
MRR 0.881; held-out Recall@1 73.3%. No provider errors. The six top-one misses
include U-3 versus U-6, nominal versus real/PPP GDP, and income versus cash flow.
These are useful failure cases, not reasons to change gold labels. The held-out
set has now been measured; future tuning uses development only, with a new held-out
set needed for an unbiased subsequent release decision.

At that milestone, out of scope: live question-time acquisition, Exa integration, generalized
SQL planning/execution, multi-product joins and a calibrated no-match decision.
Next: human semantic-label review and development-only retrieval diagnosis,
then separately design execution for a human-selected product.

## Milestone 2 — Retrieval Evaluation (original specification)

Create a benchmark of at least 30 questions, including questions answerable by
each product, ambiguous questions, questions needing unavailable fields or time
coverage, questions requiring more than one product, and questions with no match.
Keep a held-out portion separate from questions used to tune metadata or prompts.

Each item:

```json
{
  "question": "...",
  "expected_data_products": ["..."],
  "expected_outcome": "select"
}
```

For abstention cases, use an empty `expected_data_products` list and
`"expected_outcome": "abstain"`. An answerable case names one expected product.

Measure:

- Recall@1

- Recall@3 (diagnostic only with a three-product catalog)

- MRR

- no-match and multi-product abstention accuracy

- latency

- abstention accuracy and false selection rate

Add failure logging.

Important:

Track metadata-related failures separately from embedding/model failures.

## Milestone 3 — Dataset Selection Agent

Given:

- user question

- ranked retrieved products

- available datasets

- descriptions/business context

Return:

- one product/dataset candidate or abstention

- reasoning summary

- reason grounded in the manifest, including coverage limits

Add human validation:

- Continue

- Remove source

- Choose another source

- Stop when no suitable source exists

Do not generate SQL before validation. Persist the confirmed product ID and
manifest version with the run; reject stale confirmations if that version changes.

## Milestone 4 — Schema / Field Selection

Given:

- validated dataset

- schema

- user question

Return:

- required fields

- derived concepts

- relationships/joins if needed

Example:

```text

operating_margin =

operating_income / revenue

```

Evaluate:

- field precision

- field recall

## Milestone 5 — SQL Generation

Generate DuckDB-compatible SQL.

Requirements:

- use only validated tables/fields

- validate syntax before execution

- enforce read-only, allowlisted tables/columns, timeout, and result-row limit

- return generated SQL visibly

- surface execution errors

Measure:

- execution success

- result correctness

- retry count

- latency

## Milestone 6 — End-to-End Agent

Connect:

```text

question

→ retrieve product

→ select dataset

→ validate

→ select fields

→ generate SQL

→ execute

→ answer

```

Add trace UI showing every stage.

The trace is a major part of the demo.

## Milestone 7 — Metadata Quality Experiment

Create an experiment where retrieval quality changes based on metadata quality.

Example:

Bad:

```text

Corporate information.

```

Good:

```text

Quarterly and annual financial statements including

revenue, gross profit, operating income, net income,

and balance-sheet metrics.

```

Measure retrieval before/after.

Add a developer-facing insight:

> Retrieval quality can fail because the metadata is weak, not because the embedding model is bad.

## Milestone 8 — Exa External Discovery

Only add after the internal catalog works.

Offer Exa when:

- the catalog cannot answer the question under the V1 query contract

- no source covers required concepts

- user explicitly requests external discovery

Use Exa to find:

- authoritative APIs

- public datasets

- technical documentation

Do not automatically ingest arbitrary sources. External discovery first yields
cited source-finding advice. A user-approved public CSV/ZIP can support one-off
analysis as a temporary snapshot. Permanent publication requires a separate
reviewed registration action and manifest mapping.

Return candidate source + extracted metadata for user approval.

## Milestone 9 — V2 Embedding Infrastructure

Replace hosted embeddings with:

- Qwen3-Embedding

- AWS SageMaker endpoint

Keep downstream architecture unchanged.

Benchmark V1 vs V2 on:

- retrieval quality

- latency

- cost

- operational complexity

Document whether self-hosting is actually worth it.

## Evaluation Harness

Maintain stage-level metrics.

### Retrieval

- Recall@1

- Recall@3

- MRR

### Dataset Selection

- accuracy

- top-3 accuracy

### Schema Selection

- precision

- recall

### SQL

- execution success

- result correctness

### End-to-End

- task success

- latency

- LLM cost

- embedding cost

- retries

## Logging / Tracing

Every run should record:

```json
{
  "question": "",
  "retrieved_products": [],
  "selection_outcome": "selected_or_abstained",
  "selected_product_id": "",
  "manifest_version": 1,
  "human_confirmation": "confirmed_or_stopped",
  "selected_fields": [],
  "generated_sql": "",
  "execution_result": "",
  "source_snapshot_date": "",
  "latency_ms": {},
  "cost": {},
  "success": true
}
```

Make traces inspectable in the UI.

## Non-Goals

Do not:

- recreate proprietary Goldman code

- copy internal prompts

- reproduce internal schemas or metadata

- use internal terminology such as PMCD in the public implementation

- rebuild a production lakehouse

- add many agent frameworks before the core flow works

- optimize infrastructure before validating the product

## Codex Working Rules

1. Build the smallest working vertical slice first.

2. Keep TypeScript for the Next.js UI and its browser-facing API proxy.

3. Use Python/FastAPI for retrieval, provider orchestration, ingestion, evaluation, and data access.

4. Add tests with every major module.

5. Prefer explicit interfaces over framework-heavy abstractions.

6. Log intermediate agent decisions.

7. Keep all provider integrations behind adapters.

8. Keep evaluation code separate from production agent code.

9. Do not introduce SageMaker until V1 passes the benchmark.

10. Update this file when architecture decisions change.

## First Build Order

1. Scaffold repo and define/validate the manifest schema.
2. Acquire and document one real local Parquet snapshot with a manifest.
3. Build the fixed-product vertical slice through the UI, human confirmation,
   schema selection, SQL execution, answer, and trace.
4. Acquire and document the other two real Parquet snapshots and manifests.
5. Create the 30-question evaluation set, including held-out and abstention cases.
6. Add embedding generation, idempotent SingleStore ingestion, and retrieval.
7. Measure retrieval and abstention baselines on the evaluation set.
8. Add product selection from ranked candidates and the no-suitable-source path.
9. Run end-to-end evaluation and the metadata-quality experiment.
10. Add Exa source-finding advice, approved one-off CSV/ZIP analysis, then separately reviewed catalog registration.
11. Add SageMaker/Qwen V2 only after V1 meets its benchmark criteria.

## Definition of V1 Done

V1 is complete when a user can:

1. ask a question answerable by one of the 15 local sample products

2. see the top retrieved data products

3. see/select the proposed dataset

4. validate the choice

5. see selected fields

6. inspect generated SQL

7. execute the SQL

8. receive an answer

9. inspect the full trace

10. run the benchmark and view retrieval/task metrics
11. receive a clear abstention for no-match or multi-product questions

Before implementation, set numeric pass criteria for held-out retrieval,
abstention, answer correctness, latency, and cost. Report each separately;
execution success alone does not establish answer correctness.

Do not move to V2 infrastructure work before this works reliably.

### Human-in-the-loop dataset-choice review (implemented)

The `/benchmark-review` page reveals agent-authored expected choices and reasons
on request, then accepts approval, correction, wording feedback, or uncertainty.
Local SQLite retains shared review state and append-only revision history;
benchmark/catalog fingerprints prevent stale submissions. Reviews are proposals,
not automatic edits to labels or metadata. Numerical-answer review is excluded.

TODO / future improvements: separate reviewer histories, disagreement resolution,
explicit label promotion, and an approval queue for metadata improvements with
versioning, re-embedding and development-only evaluation. Held-out feedback must
not be used for metadata tuning. Add authentication before any public deployment.

### Single-product SQL planning and execution (implemented)

Human source confirmation now leads to model-generated fields/formulas and visible
DuckDB SQL, followed by a separate SQL approval and local result table. All 15
products use the same single-table engine. The old fixed-template endpoints,
execution code, runner and baseline-specific tests have been removed.

The planner defaults to configurable `gpt-4.1-mini`, using structured Responses
output from question + metadata/schema only. Fail-closed SQLGlot grammar and
DuckDB binding validate proposals. SQL and metadata/snapshot fingerprints are saved
locally; execute accepts the saved plan ID, rejects drift, and never regenerates
or accepts arbitrary browser SQL. Plans expire after one hour.

Disposable DuckDB execution uses external access disabled, locked configuration,
256 MB engine memory, no disk spill, a 5-second timeout and 500-result-row cap.
Ratios must guard zero denominators. Empty results, truncation, provider errors
and execution failures are explicit. No joins, CTEs, subqueries, windows, live
acquisition or automatic repair loops. Safety validation is not semantic accuracy.

Evaluation is separate from the retrieval benchmark: offline adversarial tests
and real snapshot gold values for all 15 products, plus 22 development-only live
model smoke cases. Neither held-out retrieval labels nor metadata are changed.
Next: human review of generated period/unit/formula interpretations, a richer
SQL gold-result benchmark, and repeatability/latency/cost release criteria.

### Unified analysis UI (implemented)

The home page now has one question input and four numbered stages: ask/compare,
review/confirm source, generate/review SQL, and results. The unemployment example
fills the same input. Schema details are collapsed; definitions, coverage, units,
source and snapshot remain visible. Source confirmation and SQL approval remain
separate gates. Question/source/version changes clear downstream state, and late
responses cannot restore it. SQL results retain provenance and their own trace;
the obsolete baseline form and baseline-only sidebar trace are removed.

Benchmark review remains a separate navigation link. The UI consolidation did
not change the database. Subsequent cleanup retired `/runs/preview` and
`/runs/execute`; `/sql-runs/*` remains the saved-plan API. The home page now uses
the persisted `/workflows` API described below.
`npm test` adds offline UI
interaction coverage using mocked APIs, separate from live model evaluation.

### LangGraph-guided source selection (implemented)

LangGraph StateGraph owns the full local workflow and human pauses using a SQLite
checkpointer in `.local/workflows.sqlite3`. Bounded Source Advisor, SQL Planner
and External Discovery specialists have typed boundaries; catalog/fingerprint
checks, routing, approvals and DuckDB execution are deterministic. Human interrupts
are separate from provider calls so resume never repeats a completed specialist.
Existing SQL APIs and their saved-plan store/safety checks remain shared services.

The advisor assesses all 15 public metadata/schema summaries, independently of
SingleStore, and returns at most three explained recommendations, one clarification,
or no local fit with data-gap/unsupported-operation distinction. Manual browsing
bypasses advice. Planner abstention offers explicit recovery excluding rejected
sources. Recommendations, source selection and recovery never execute SQL.

Exa discovery is an explicit consent-based branch. One bounded search returns
up to five links and excerpts. A structured external advisor call ranks only
those candidates, distinguishes measure, geography, period and granularity,
and abstains when evidence is inadequate. Source-finding advice is not a
numerical answer and does not claim independent page verification.

The user can select one candidate for CSV/ZIP one-off analysis. Only that page
is inspected for supported links; the user approves the exact file URL before
a bounded, pinned public-HTTPS download. CSV/ZIP validation creates a temporary
Parquet snapshot under the run; schema, sample rows, observed units and coverage
are shown before SQL planning. The existing restricted planner and saved-plan
approval/execution path accepts either a catalog product or this immutable
temporary snapshot. Numerical claims derive from executed rows, not snippets.
Cancellation and expiry remove run-scoped files.

After a successful one-off result, reviewed name, geography, measure, unit,
coverage and key fields can be explicitly registered through the existing
ingestion pipeline. This publishes a versioned snapshot and manifest plus a
CSV/ZIP refresh recipe, without a new download or automatic SingleStore index.
API-based acquisition, PDFs and HTML charts remain later improvements.

POST /workflows creates; GET inspects; POST /workflows/{id}/actions resumes through
typed, stage-validated human actions; DELETE cancels. Revisions, request IDs and
per-run locks prevent conflicting actions and repeat completed requests. Late
responses are ignored; question edits retire old runs; source/catalog changes
invalidate downstream artifacts and approvals. The current tab stores only its
run ID and restores paused/completed state on refresh, without provider calls.
Run creation returns its ID before assessment; the UI polls persisted state
while a local background worker runs. Interrupted work needs explicit
retry; arbitrary process crashes do not have exactly-once provider semantics.
SQL-plan TTL stays one hour; run TTL is 24 hours with access-time checkpoint
pruning. Traces expose status, duration, provider/model and token counts only.

Offline graph/API/UI tests mock providers and exercise gates, refresh/restart,
recovery, consent, invalid outputs, conflicts, cancellation, drift, expiration and
failures. `evals.source_advisor` evaluates development cases separately; live calls
require --live and a new report filename. No held-out tuning or automatic label
updates. Runtime harness selection remains separate from LangGraph orchestration.
