# DataScout

DataScout finds a data product before it runs a query. The current build is the
first working local flow: real FRED and World Bank snapshots, a human source
confirmation step, a bounded DuckDB query, and a visible answer/SQL trace.

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

`DATASCOUT_RETRIEVAL_BACKEND=singlestore` enables semantic retrieval in the UI
question flow. The subsequent selector still uses deterministic measure/coverage
checks. Set the backend to `local` for the offline baseline. The API also exposes
`POST /retrieval/search` with `question` and optional `top_k` (default 3).

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

The [World Bank snapshot](data/sample_datasets/world_bank_us_gdp_per_capita/snapshot.json)
contains annual U.S. GDP per capita observations for 2000–2024 from the
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

The current flow handles monthly U.S. unemployment and annual U.S. GDP-per-capita
questions within their snapshot ranges. SingleStore semantic retrieval precedes
deterministic product selection when enabled. Apple's SEC snapshot is acquired
and cataloged; its question-execution adapter, LLM selection,
the larger benchmark, and Exa discovery are next in [PLAN.md](PLAN.md).
