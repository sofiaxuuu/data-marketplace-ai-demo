# Approved external CSV/ZIP processing

DataScout treats a discovered link as evidence about a possible source, not as a
queryable dataset. A user must select a candidate, review the exact CSV/ZIP link,
and approve that link before any data download. A local file in `data/` is not
automatically imported, and the current UI does not accept pasted file URLs.

## From approval to a temporary snapshot

1. **Acquire safely.** The server accepts public HTTPS URLs only, resolves and
   pins a public IP address for the request, verifies TLS, and does not follow
   redirects. The downloaded response is capped at 25 MB. A ZIP must contain
   exactly one CSV entry; archive paths and unsupported content are rejected.
2. **Extract within the run.** The bounded downloaded ZIP can remain in memory,
   but its CSV is decompressed in chunks into
   `.local/external-runs/<run-id>/incoming-*/source.csv`. Both the ZIP's declared
   size and the actual extracted byte count are checked against a 350 MB cap.
   A failed attempt removes the staging directory rather than leaving a partial
   queryable file.
3. **Inspect and normalize.** DuckDB reads the staged CSV from disk. DataScout
   checks the schema, safe column names, at most one million rows, an identifiable
   date range, and any observable unit field. It writes a single-table Parquet
   snapshot. A header beginning with a digit gets a safe `column_` prefix;
   collisions still fail for review. File hashes are computed in chunks.
4. **Publish only after validation.** The validated `source.csv` and
   `snapshot.parquet` are moved into the run directory. The LangGraph checkpoint
   retains metadata, fingerprints, and the run ID—not the large CSV or a
   browser-supplied filesystem path. Refreshing the page reuses this snapshot.

The snapshot is temporary and belongs to one workflow, not the catalog. The
user still inspects coverage, units, geography, and reporting basis before
confirming it. A file containing many places or monitors does not imply a
single citywide observation. Only after source confirmation does the restricted
SQL planner run; execution has its own approval gate. Numerical answers come
from executed rows, not search snippets.

## Optional repeatable registration

After a successful one-off analysis, the user can separately review a proposed
product name, measure, units, geography, date column, and unique observation key.
Registration verifies the retained CSV hash, then uses a path-based ingestion
route: DuckDB scans the CSV from disk, casts the reviewed columns, and checks row
count, key uniqueness, and coverage before publishing a versioned immutable
snapshot and manifest. Hashing and file copies are chunked; the expanded CSV is
not materialized as one Python object. A CSV/ZIP refresh recipe is saved, but
refresh remains an explicit later command. Registration does not redownload the
file or index metadata in SingleStore.

## Lifetime and limits

Run files survive page refreshes for up to 24 hours. Cancellation, replacing the
temporary source, or run expiry removes them; an immutable registered snapshot
is separate and remains in the catalog. Failed validation never publishes a
product. Retrying an uncertain download is explicit and uses the approved exact
URL. These bounds support moderately large CSVs, not arbitrary archives or
multi-file datasets. PDFs, HTML-only charts, and API responses are outside this
file-processing path.

Ordinary tests use mocked downloads and small fixtures. The optional local-ZIP
integration test uses a separately supplied archive, performs real temporary
snapshot creation and catalog registration in an isolated test directory, and
does not change the live catalog. See the command in the README's Verify section.
