# DataScout

DataScout has 15 real, overlapping data products. A Source Advisor recommends
local products with reasons; you inspect and confirm one, review SQL, approve
execution and see local results. LangGraph persists human pauses so refreshing
the current tab restores its run. Explicit Exa discovery finds external candidate
sources, never SQL-ready tables. Samples are local Parquet; descriptive metadata
is also embedded in SingleStore for retrieval and evaluation.

## Run locally

Requires Python 3.12+, Node.js 20+, and npm.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
PYTHONPATH=python .venv/bin/uvicorn datascout.main:app --reload --port 8000
```

In another terminal:

```bash
npm run dev
```

Open `http://localhost:3000` and try “What was the U.S. unemployment rate in
April 2020?” or “What was U.S. GDP per capita in 2020?” The UI proxies requests
to the local Python API. Set
`DATASCOUT_API_URL` if the API uses another host or port.

## SingleStore connection

The ignored local `.env` file contains the endpoint settings. Fill in
`SINGLESTORE_PASSWORD` locally (quote the value if it contains spaces or `#`).
The Python adapter uses separate connection arguments, so usernames with spaces
and passwords with special characters do not need URL encoding. TLS certificate
and hostname verification are enabled.

`SINGLESTORE_SSL_CA=singlestore_bundle.pem` uses the certificate bundle downloaded
from SingleStore. Relative certificate paths are resolved from the repository
root. If this setting is omitted, the client uses the standard Certifi CA bundle.

```bash
.venv/bin/pip install -r requirements.txt
PYTHONPATH=python .venv/bin/python -m datascout.singlestore check
```

The check runs a read-only query and prints connection status, database, and
server version.

## Embeddings and retrieval

Set `OPENAI_API_KEY` in the ignored local `.env`. V1 uses
`text-embedding-3-small` with 1,536 dimensions. Only descriptive manifest metadata
is sent to OpenAI; Parquet rows and local file paths are excluded.

```bash
PYTHONPATH=python .venv/bin/python -m datascout.retrieval setup
PYTHONPATH=python .venv/bin/python -m datascout.retrieval ingest
PYTHONPATH=python .venv/bin/python -m datascout.retrieval search --question "What was U.S. GDP per capita in 2020?"
```

The `datascout_products_v1` table holds metadata, manifest version, content hash,
model name, and a normalized vector. Ingestion updates changed products and skips
unchanged ones. Search uses exact cosine similarity and excludes stale/deleted
local manifests. Scores rank candidates; they are not calibrated confidence.

The home page’s advisor assesses all local metadata/schema without depending on
SingleStore. Recommendations require a human choice; inspection alone never
generates or executes SQL. Manual catalog selection bypasses the advisor.
The API exposes `POST /retrieval/search` with
`question` and optional `top_k` (default 3, maximum 10), returning comparison
metadata and execution-support status. `GET /catalog` exposes all products and
schemas, without repository paths. Catalog changes invalidate UI choices on
the next refresh (every 30 seconds or on window focus).

## Data provenance

The committed Parquet file contains 84 monthly observations from January 2018
through December 2024 from [FRED series UNRATE](https://fred.stlouisfed.org/series/UNRATE).
[`snapshot.json`](data/sample_datasets/fred_unemployment/snapshot.json) records
the download date, source checksum, and Parquet checksum. To rebuild it from the
official CSV, run:

```bash
.venv/bin/python scripts/build_fred_snapshot.py
```

This is a one-time build operation. User questions never call FRED. A rebuild
can change values if the source revises its historical series.

The active World Bank products all cover **2015–2024**. The original
[archived snapshot](data/sample_datasets/world_bank_us_gdp_per_capita/snapshot.json)
retains its 2000–2024 observations from the
[World Bank Indicators API](https://api.worldbank.org/v2/country/USA/indicator/NY.GDP.PCAP.CD?format=json).
Rebuild it with `.venv/bin/python scripts/build_world_bank_snapshot.py`.

### SEC acquisition with EdgarTools

Set `EDGAR_IDENTITY="Your Name your-real-email@example.com"` in the ignored
local `.env`. This contact identity is sent to SEC during acquisition; it is
not included in catalog metadata or embeddings.

```bash
.venv/bin/python scripts/build_sec_snapshot.py
```

The script pins Apple's 2024 10-K, accession `0000320193-24-000123`, and
extracts consolidated annual revenue, gross profit, operating income and net
income for FY2022–FY2024. Fiscal period dates are retained. Values use XBRL
whole USD, not the report's displayed millions. Segmented, quarterly, non-USD,
missing or conflicting facts are rejected rather than guessed.

The original verified sample remains under
`data/sample_datasets/sec_apple_income_statement/`. The compatibility command
now delegates to the shared pipeline and writes immutable snapshots under
`data/snapshots/`, then publishes the manifest. To replay the original without SEC:

```bash
.venv/bin/python scripts/build_sec_snapshot.py --input-json data/sample_datasets/sec_apple_income_statement/source_facts.json
```

Acquisition is separate from question execution. Company, filing, concepts and
fiscal periods now come from a reviewed recipe, not an Apple-specific script.
The verified snapshot is registered in the local catalog
and can be indexed with the existing retrieval ingestion command. The SEC
question-execution adapter is not yet implemented.

## Shared configurable ingestion

Datasets are YAML configurations in `data/recipes/`. Generic CSV and REST/JSON
adapters can onboard other public providers without new pipeline code; SEC uses
a reusable EdgarTools/XBRL reader. See [the ingestion guide](docs/ingestion.md)
for recipe fields, supported capabilities and safety boundaries.

```bash
PYTHONPATH=python .venv/bin/python -m datascout.ingestion list
PYTHONPATH=python .venv/bin/python -m datascout.ingestion plan fred_unemployment
# Explicit acquisition; no active catalog change unless --publish is included:
PYTHONPATH=python .venv/bin/python -m datascout.ingestion build fred_unemployment --download
# Register and index only after an explicit acquisition/replay command:
PYTHONPATH=python .venv/bin/python -m datascout.ingestion build fred_unemployment --download --publish --index
```

The pipeline retains raw sources and immutable Parquet snapshots with hashes.
Publication atomically replaces a validated manifest and increments its version
when registration changes. Failed acquisition/validation preserves the current
catalog. Failed indexing preserves the new local snapshot and can be retried
with the existing retrieval ingestion command. The old script entrypoints still
work, but now publish through this pipeline; offline CSV/JSON inputs require
their original `--retrieved-at YYYY-MM-DD` unless a matching sidecar supplies it.

Adding a recipe enables acquisition/catalog metadata, not automatic question
execution. Exa onboarding/acquisition and arbitrary SQL sources remain future
work. Questions never initiate live source acquisition; external searches require
an explicit user action and return candidate links only.

## Verify

```bash
PYTHONPATH=python .venv/bin/pytest -q
npm test
npm run typecheck
npm run build
```

## Current scope

| Family | Products | Active sample coverage |
|---|---|---|
| FRED | Adjusted U-3, unadjusted U-3, unemployment count, adjusted U-6 | Monthly 2018–2024 |
| World Bank | U.S. nominal per-capita GDP, total nominal GDP, real per-capita GDP, PPP per-capita GDP; Canadian nominal per-capita GDP | Annual 2015–2024 |
| Apple SEC | Annual income, cash flow, balance sheet; Q3 income | Income/cash flow FY2022–2024; balance FY2023–2024; Q3 FY2023/FY2024 only |
| Microsoft SEC | Annual income and cash flow | FY2022–2024 |

SEC facts come from pinned 2024 filings, not latest-filing lookups. Fiscal dates,
instant balance-sheet values and exact three-month quarters are kept distinct.
Existing archived snapshots remain untouched. Browse recipes with the ingestion
CLI; `.venv/bin/python scripts/build_catalog.py --download` publishes **missing**
reviewed products only and never silently refreshes existing ones.

Human-confirmed single-table SQL planning and execution cover all 15 products.
Broader SQL operations, calibrated automatic source decisions and acquisition
from discovered sources remain future work. Catalog registration alone does not
establish that a product can answer every question.

## Competing-product evaluation

```bash
PYTHONPATH=python .venv/bin/python -m evals.expanded --validate-only
PYTHONPATH=python .venv/bin/python -m evals.expanded --split development --output evals/results/NEW_REPORT_NAME.json
# Only run held-out after freezing changes; never tune against held-out labels:
PYTHONPATH=python .venv/bin/python -m evals.expanded --split held_out
```

The benchmark has 30 answerable questions, 15 clarification cases and 15
unavailable/multi-source requests, split evenly into development and held-out.
It locks all 15 manifest versions, metadata hashes and snapshot hashes. Positive
labels have mechanically checked schema/period evidence; semantic labels are
agent-authored and still require human review. This is not an official
FinSearchComp score or a numeric-answer benchmark.

The first untuned [live baseline](evals/results/expanded-baseline-v2.json) achieved
80% Recall@1, 96.7% Recall@3 and 100% Recall@5 across answerable questions;
held-out Recall@1 was 73.3%. Full-catalog MRR was 0.881. All annotated plausible
clarification candidates appeared in the top five. Unavailable cases are
diagnostics—not an abstention accuracy score, because a retriever always ranks.

The original 36-case benchmark and its frozen manifests are historical artifacts;
its fixed-template runner has been removed. Use the expanded retrieval benchmark
and SQL evaluation for the current workflow.

### Human benchmark review

Open `/benchmark-review` (or click **Review benchmark** on the home page).
Review one question at a time: reveal the agent-authored expected dataset choice
and reason, then approve, propose a correction, flag wording, or mark not sure.
Search the catalog when proposing a different dataset. No SQL, embeddings, or
live source calls are made. Numerical answers are outside this review's scope.

Reviews are shared locally, stored with append-only revisions in the ignored
`.local/benchmark-reviews.sqlite3` database. Keep this file to retain feedback;
it is not included in git. Saves require the frozen benchmark/catalog versions
and reject concurrent overwrites. Unreviewed includes stale and not-sure cases.
Feedback does **not** change benchmark labels, reports, metadata, or embeddings.
This is a trusted-local workflow without authentication; do not expose it as a
public review service.

Future improvements: separate reviewer histories and disagreement resolution;
approved label promotion; approved, versioned metadata suggestions followed by
re-embedding and evaluation. Never use held-out feedback for metadata tuning.

### Human-approved SQL across the local catalog

On the home page, enter a question in **Ask and compare sources**, choose a
candidate (or browse the catalog), and confirm that dataset fits the question.
**Generate SQL for review** produces the fields, formulas, interpretation, units
and query. Approve the SQL separately, then **Execute approved SQL** shows the
local result table, source, snapshot version, timing and trace. The four numbered
stages share one question input; an unemployment example fills that input without
making a request. Detailed schema inspection is collapsed by default. Changing
the question, source, or catalog version clears downstream plans and approvals.
Selecting or
inspecting a product alone never generates or executes a query.

The configurable planner defaults to `gpt-4.1-mini`; set `DATASCOUT_SQL_MODEL` in
the local environment to change it. It uses `OPENAI_API_KEY` and the Responses API
with structured output. Only the question and selected product metadata/schema
are transmitted—not Parquet rows or local paths. Returned numbers are displayed
from DuckDB results, not invented by a second language-model answer call.

All 15 products share one engine, without per-dataset SQL adapters. The first
grammar supports lookups, ordered time series, aggregates, conditional comparisons
and ratios over **one registered table in one confirmed product**. It rejects
joins, subqueries, CTEs, windows, arbitrary functions, writes, external reads,
unknown fields, and unguarded division. Schema binding and parser checks establish
safety/validity, not business correctness: review periods, units and formulas.
Requests needing another source or missing concepts/coverage must be clarified
or declined by the planner; this semantic decision is not guaranteed by SQL validation.

Execution materializes only trusted snapshot tables in a disposable DuckDB
process, disables external access, locks configuration, limits DuckDB memory to
256 MB, disables disk spill, and imposes a 5-second process timeout. Results are
limited to 500 rows with an explicit truncation flag. Empty results are not zero.
Generated plans bind question, product version, metadata/snapshot fingerprint and
SQL in `.local/sql-runs.sqlite3`; execution accepts only a saved plan ID and
approval, not browser-supplied SQL. Plans expire after one hour; changed products
require fresh confirmation. No automatic SQL repairs/retries or live acquisition.

API: `POST /sql-runs/generate` takes `question`, `product_id`, `manifest_version`
and `confirmed: true`; `POST /sql-runs/execute` takes `run_id` and `approved: true`.
Next.js proxies these through `/api/sql-runs/*`. Catalog `execution_supported`
and `execution_scope` describe the single-product SQL workflow and its limitations.
These endpoints remain available to existing callers; the home page uses the
persisted workflow API below and shares the same SQL service implementation.

Run offline safety/snapshot tests with
`PYTHONPATH=python .venv/bin/python -m pytest tests/test_sql_runs.py -q`.
The separate development-only SQL smoke evaluation calls OpenAI explicitly:

```bash
PYTHONPATH=python .venv/bin/python -m evals.sql_execution --output evals/results/NEW_SQL_REPORT.json
```

Its 22 cases cover all 15 confirmed products, ratios, signed cash flows, unit
conversion, missing periods/concepts, multiple sources and ambiguity. Numeric
result containment is only a smoke check—not full semantic accuracy or a held-out
benchmark. Reports never overwrite prior runs or alter retrieval labels.

The [current development smoke report](evals/results/sql-development-v3.json)
passed 22/22. Earlier reports preserve provider and table-qualification failures;
the final prompt explicitly forbids schema/product qualification. The ratio case
now explicitly requests no rounding to match its full-precision numeric label.

`npm test` runs offline frontend interaction tests for the unified workflow:
one question input, approval gates, macroeconomic/SEC source choices, state resets,
late responses, errors, clarification, empty results, and truncation. These use
mocked API responses and do not call OpenAI or SingleStore.

## LangGraph workflow and external discovery

The graph coordinates three specialists: Source Advisor, SQL Planner and External
Discovery. Catalog checks, routing, approval gates and execution are deterministic.
Specialists cannot approve their own proposals. Human interrupts are separate from
provider/execution nodes, so resuming a pause does not repeat a completed call.
No CrewAI, autonomous supervisor, agent SDK integration or LangSmith account is
required (LangGraph installs its own transitive dependencies).

The Source Advisor makes one bounded structured OpenAI call with all 15 products’
public metadata/schema, never sample rows, local paths, keys or benchmark labels.
`DATASCOUT_SOURCE_ADVISOR_MODEL` overrides the existing SQL-model default.
Advice distinguishes recommendations, ambiguity, missing local data and
unsupported operations. Planner abstention offers explicit local recovery and
excludes previously rejected products. Errors and empty results do not trigger
automatic external search.

“None of these fit” or a data-gap assessment offers external discovery. Clicking
“Find external sources” sends the question to Exa using server-side `EXA_API_KEY`.
One search returns at most five HTTPS candidate links and bounded excerpts.
Publisher authority/suitability need human review; unknown coverage, units,
access and terms remain unknown. Candidates cannot enter SQL, ingestion,
registration or indexing. The backend never fetches arbitrary discovered URLs.

API (proxied under `/api/workflows`):

- `POST /workflows`: UUID `request_id`, question, optional product ID/version for manual choice.
- `GET /workflows/{id}`: read authoritative stage, artifacts, allowed actions and sanitized trace; no provider calls.
- `POST /workflows/{id}/actions`: UUID `request_id`, `expected_revision`, and a typed `action`.
- `DELETE /workflows/{id}`: cancel the run and suppress late results.

Actions are `select_source`, `choose_again`, `confirm_source` (`confirmed: true`),
`approve_sql` (`approved: true`, exact `plan_id`), `recover_local`, `none_fit`,
`discover_external` (`consent: true`) and `retry`. Source selection needs product
ID/version. Only currently allowed actions are accepted; duplicates replay saved
responses without repeating work and conflicting revisions return HTTP 409.

`.local/workflows.sqlite3` holds checkpoints/action records; the existing
`.local/sql-runs.sqlite3` remains the SQL-plan authority. The current tab stores
only its run ID in sessionStorage. Paused/completed runs restore after refresh;
reads never retry uncertain provider work. A crashed operation requires explicit
retry and can repeat a call whose outcome was uncertain—not an exactly-once claim.
Run creation returns its ID before the initial assessment, and the UI polls the
persisted state while the local background worker assesses the catalog.
Question edits retire the old run; source changes clear downstream approvals and
results. Catalog drift invalidates a run. SQL plans expire after one hour;
workflow checkpoints/results expire after 24 hours and are pruned on later access.
Traces show stage, duration, model/provider and available token counts, not hidden
reasoning or estimated cost. Local SQLite and unguessable run IDs are development
defaults, not multi-user authentication: add authentication and production-grade
storage/worker management before public deployment.

Development-only advisor evaluation (no held-out tuning):

```bash
PYTHONPATH=python .venv/bin/python -m evals.source_advisor
# Explicitly calls OpenAI; choose a new output filename:
PYTHONPATH=python .venv/bin/python -m evals.source_advisor --live --output evals/results/NEW_ADVISOR_REPORT.json
```

The default command validates frozen benchmark evidence without model calls.
Live grading checks advisor outcomes and first-choice products against development
labels; it is not numerical-answer validation or held-out accuracy.
