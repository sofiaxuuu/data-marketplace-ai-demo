"""Offline extraction safety tests; these synthetic facts are not sample data."""

from pathlib import Path

import duckdb
import pytest

from datascout.ingestion.adapters import sec_records
from datascout.ingestion.models import load_recipe
from datascout.ingestion.pipeline import build_snapshot

recipe = load_recipe("sec_apple_income_statement")
source = recipe.source
OUTPUT = Path(__file__).resolve().parents[1] / "data/sample_datasets/sec_apple_income_statement"


def extract_rows(items):
    return [tuple(row[col.name] for col in recipe.columns) for row in sec_records(source, items)]


def facts():
    result = []
    for year, end in source.period_ends.items():
        for concept in source.metrics.values():
            result.append({"concept": concept, "period_type": "duration", "period_start": f"{year - 1}-09-26", "period_end": end.isoformat(), "currency": "USD", "entity_identifier": "0000320193", "numeric_value": 100})
    return result


def test_ignores_segments_quarters_and_other_currencies():
    items = facts()
    for overrides in [{"dim_product": "services"}, {"period_start": "2024-07-01"}, {"currency": "EUR"}]:
        items.append({**items[-1], **overrides, "numeric_value": 999})
    assert [row[0] for row in extract_rows(items)] == [2022, 2023, 2024]
    assert extract_rows(items)[-1][-1] == 100


def test_conflicting_annual_totals_fail():
    items = facts()
    items.append({**items[0], "numeric_value": 200})
    with pytest.raises(ValueError, match="unambiguous"):
        extract_rows(items)


def test_missing_metric_fails():
    with pytest.raises(ValueError, match="unambiguous"):
        extract_rows(facts()[1:])


def test_offline_snapshot_preserves_dollar_units(tmp_path):
    import json

    payload = {"cik": source.cik, "accession": source.accession, "retrieved_at": "2026-09-25", "edgartools_version": "test", "facts": facts()}
    result = build_snapshot(recipe, json.dumps(payload).encode(), retrieved_at="2026-09-25", root=tmp_path)
    metadata = result["metadata"]
    assert metadata["rows"] == 3
    with duckdb.connect() as conn:
        rows = conn.execute("SELECT revenue_usd FROM read_parquet(?)", [str(tmp_path / result["snapshot"] / "annual_income.parquet")]).fetchall()
    assert rows == [(100,), (100,), (100,)]  # No million-dollar scaling.


def test_real_snapshot_matches_filing_statement():
    # Independently checked against the FY2024 10-K's statements of operations.
    with duckdb.connect() as conn:
        rows = conn.execute("SELECT fiscal_year, revenue_usd, gross_profit_usd, operating_income_usd, net_income_usd FROM read_parquet(?) ORDER BY fiscal_year", [str(OUTPUT / "income_statement.parquet")]).fetchall()
    assert rows == [
        (2022, 394328000000, 170782000000, 119437000000, 99803000000),
        (2023, 383285000000, 169148000000, 114301000000, 96995000000),
        (2024, 391035000000, 180683000000, 123216000000, 93736000000),
    ]
