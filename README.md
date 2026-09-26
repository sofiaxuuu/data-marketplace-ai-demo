# DataScout

DataScout now has 15 real, overlapping data products. Search ranks candidates;
you compare their definitions, units and periods and explicitly choose one to
inspect. Samples are local Parquet; descriptive metadata is embedded in
SingleStore. A separate limited baseline runs fixed FRED/World Bank DuckDB queries.

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

The candidate inspector always uses SingleStore search and requires a human
choice; it never executes SQL. The API exposes `POST /retrieval/search` with
`question` and optional `top_k` (default 3, maximum 10), returning comparison
metadata and execution-support status. `GET /catalog` exposes all products and
schemas, without repository paths. Catalog changes invalidate UI choices on
the next refresh (every 30 seconds or on window focus).

`DATASCOUT_RETRIEVAL_BACKEND=singlestore` optionally enables retrieval in the
separate limited baseline workflow. That deterministic selector still supports
only adjusted U-3 unemployment and current-USD U.S. GDP per capita; it is not
the selector for the expanded catalog. Use `local` for its offline mode.

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
execution. Exa onboarding, arbitrary SQL sources and generalized query planning
remain future work. Questions still never initiate live source acquisition.

## Verify

```bash
PYTHONPATH=python .venv/bin/pytest -q
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

Generalized SQL execution, automatic selection/abstention and Exa discovery
remain future work. Catalog registration does not enable question execution.

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

The original 36-case benchmark remains a three-product **local-only** smoke
test, pinned to archived manifests under `evals/legacy_catalog`. Run
`PYTHONPATH=python .venv/bin/python -m evals.run --validate-only` or
`PYTHONPATH=python .venv/bin/python -m evals.run --mode local --execute`.
Its labels do not evaluate the expanded index.

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
