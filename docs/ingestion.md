# Configurable ingestion

Add a reviewed YAML file named `<dataset_id>.yaml` under `data/recipes/`. No
provider or dataset registry needs editing. The recipes for the three initial
datasets are examples of the supported formats, not a limit on providers.

## Minimal CSV recipe

This example is illustrative, not a registered sample or a working endpoint:

```yaml
recipe_version: 1
id: regional_observations
name: Regional annual observations
description: Annual measurements from a reviewed public source
business_context: Annual coverage from 2022 through 2023
concepts: [regional measurements]
table_id: observations
source_name: Reviewed provider
source_url: https://data.example.org/documentation
source:
  adapter: csv
  url: https://data.example.org/observations.csv
  allowed_hosts: [data.example.org]
columns:
  - name: year
    type: INTEGER
    description: Calendar year
    paths: [[YEAR]]
  - name: measured_value
    type: DOUBLE
    description: Annual measured value
    unit: USD
    paths: [[VALUE]]
unique_key: [year]
coverage: { column: year, start: "2022", end: "2023", frequency: annual }
expected_rows: 2
```

Use `plan <dataset_id>` to inspect, then `build <dataset_id> --download` to
acquire a validated snapshot. Add `--publish` to register it. Add `--index` to
run catalog metadata ingestion after publication; this requires SingleStore
and the configured embedding service. Inspection never fetches or writes.
The caller is responsible for reviewing access rights, source terms and recipes.

## Supported configuration

- `csv`: HTTPS or offline input; UTF-8/UTF-8-BOM, configurable delimiter. Column
  `paths` are ordered alternatives for headers, e.g. `[[DATE], [observation_date]]`.
- `rest_json`: HTTPS GET with `params`, a nested `records_path` pointing to an
  array of objects, and nested column paths. Paths are lists of string keys or
  integer array indices, not JSONPath expressions or executable code.
- JSON pagination is `none` or `page`, configured with `page_parameter`,
  `first_page`, `total_pages_path` and `max_pages`. The total represents the
  number of pages, not the highest page number. Saved JSON envelopes contain
  every page; replay rejects incomplete responses.
- `headers_env` maps an HTTP header to an environment variable containing its
  full value, e.g. `{Authorization: DATA_PROVIDER_AUTH}`. A bearer variable must
  include its `Bearer ` prefix. Credentials in URLs/query parameters and literal
  authorization headers are not supported. No environment values are persisted.
- Output types: DATE (ISO dates), INTEGER, BIGINT, DOUBLE and VARCHAR. Columns
  can be nullable; numeric `scale` is an explicit multiplier. Units/descriptions
  are reviewed metadata, not guessed from values.
- `skip_missing` uses raw-source paths and excludes explicitly missing
  observations. `missing_values` defaults to empty string and `.`. Required
  missing columns, invalid/nonfinite numbers and lossy integer casts fail.
- `filters` apply inclusive minimum/maximum bounds to normalized output columns.
  `checks` assert JSON response identity; `record_checks` assert each raw record's
  identity before skipping missing values.
- `unique_key` rejects duplicates/null keys. Coverage can require complete
  monthly, annual or bounds-only coverage. `expected_rows` is optional.
- `sec_xbrl`: configure CIK, exact accession, filing date, explicit fiscal-year
  end dates and column-to-XBRL concept mappings. The reader uses annual
  consolidated, non-dimensional USD whole-dollar facts. Conflicting totals,
  missing concepts or inconsistent period starts fail. Other currencies,
  quarterly statements and arbitrary fiscal durations are not yet supported.

## Storage, replay and publication

The same pipeline validates all adapters and writes:

```text
data/snapshots/<dataset_id>/<fingerprint>/
  <table_id>.parquet
  source.csv or source.json
  recipe.json
  snapshot.json
```

Fingerprints include pipeline version, raw-source hash, effective recipe hash
and original retrieval date. Existing snapshots are never overwritten. Builds
are locked per dataset; active manifest replacement is atomic and follows
catalog schema/checksum validation. Old snapshots remain available. Unpublished
or orphaned snapshots do not change the active catalog.

```bash
PYTHONPATH=python .venv/bin/python -m datascout.ingestion build DATASET --input PATH_TO_SOURCE --retrieved-at YYYY-MM-DD --publish
```

Replay can omit the date for a matching `snapshot.json` sidecar, or for SEC
facts that already record it. It never assumes today's date for an old file.
Use the same reviewed recipe for replay; `recipe.json` records the effective
configuration for auditing but is not executable/imported automatically.

Manifest versions increment when registration changes. Repeated publication
of the same snapshot/recipe/date is idempotent. Refreshes invalidate old
confirmation/index versions. Indexing is separate; failures do not roll back
successful local publication. Retry `python -m datascout.retrieval ingest`
with `PYTHONPATH=python`, without another source download.

## Boundaries

This is not an arbitrary web crawler. Only reviewed HTTPS hosts are accepted,
private/reserved destinations are rejected, cross-host redirects are rejected,
TLS stays enabled, and source responses are bounded to 20 MiB total. HTTP
timeouts are 30 seconds and pagination defaults to at most 100 pages. Recipes
are trusted local configuration: hostname checks are defense in depth, not a
network sandbox for executing unreviewed, adversarial recipes.

POST APIs, cursor/next-link pagination, OAuth flows, API keys in query strings,
SQL sources, scheduling and automatic Exa acquisition are not implemented yet.
New behavior needs an adapter extension; do not embed Python/SQL in YAML.

Exa will supply discovery candidates and evidence, not immediate queryable
products. Review a candidate, configure a supported adapter, explicitly acquire
and register it, then index its metadata. Existing question/SQL adapters remain
limited to their current supported products; registration alone does not add
generalized question execution.

# How it works?

Step Where the code What it does
lives
━━━━━━━━━━━━━━━━━━━━━━━━━━━ ━━━━━━━━━━━━━━━━━━ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1.  Read configuration load_recipe() Loads YAML and validates its structure.
    (python/
    datascout/
    ingestion/
    models.py:163)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
2.  Download data acquire() Handles HTTP requests, credential references and JSON pagination.
    (python/ SEC delegates to EdgarTools.
    datascout/
    ingestion/
    adapters.py:76)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
3.  Parse the source records() Turns CSV, JSON or XBRL facts into records.
    (python/
    datascout/
    ingestion/
    adapters.py:144)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
4.  Map and convert fields normalize() Renames fields, converts types, applies scaling and filters.
    (python/
    datascout/
    ingestion/
    pipeline.py:56)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
5.  Validate observations validate() Checks row count, duplicate keys and time coverage.
    (python/
    datascout/
    ingestion/
    pipeline.py:100)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
6.  Save the snapshot \_build_snapshot( Writes Parquet, source data, effective configuration and
    ) (python/ provenance/checksums.
    datascout/
    ingestion/
    pipeline.py:149)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
7.  Register the product publish_manifest Validates the new manifest, then atomically replaces the active
    () (python/ one. Only with --publish.
    datascout/
    ingestion/
    pipeline.py:216)
    ─────────────────────────── ────────────────── ─────────────────────────────────────────────────────────────────────
8.  Index metadata ingest() Embeds descriptive metadata and stores it in SingleStore. Only with
    (python/ --index.
    datascout/
    retrieval.py:60)

For a concrete example, open the FRED recipe (data/recipes/fred_unemployment.yaml:1). It specifies:

adapter: csv and the download URL.
paths: [[UNRATE]]: read the source column UNRATE.
name: unemployment_rate: rename it in our output.
type: DOUBLE, unit: percent: define its type and meaning.
Monthly coverage and an expected count of 84 observations.

Run it with:

PYTHONPATH=python .venv/bin/python -m datascout.ingestion build fred_unemployment --download --publish --index
