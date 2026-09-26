"""Competing products, SEC period safety, and retrieval-only evaluation."""

import json
from copy import deepcopy
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from datascout.catalog import catalog, local_path
from datascout.ingestion.adapters import sec_records
from datascout.ingestion.models import Recipe, load_recipe
from datascout.ingestion.pipeline import build_snapshot
from datascout.inspection import public_product
from datascout.main import app
from evals import expanded


def test_fifteen_real_products_with_consistent_comparison_metadata():
    products = catalog()
    assert len(products) == 15
    assert all(p["facets"] for p in products)
    for p in products:
        assert p["source"]["url"].startswith("https://")
        if p["id"].startswith("world_bank_"):
            assert (p["snapshot"]["start"], p["snapshot"]["end"], p["snapshot"]["rows"]) == ("2015", "2024", 10)


def test_public_catalog_has_schema_not_local_paths():
    response = TestClient(app).get("/catalog")
    assert response.status_code == 200
    products = response.json()
    assert len(products) == 15
    serialized = json.dumps(products)
    assert "data/snapshots/" not in serialized
    assert "source_sha256" not in serialized
    assert sum(p["execution_supported"] for p in products) == 2
    assert all(p["tables"][0]["columns"] for p in products)


def quarterly_facts(source):
    return [{"concept": concept, "period_type": "duration", "period_start": p.start.isoformat(),
             "period_end": p.end.isoformat(), "entity_identifier": str(source.cik), "currency": "USD", "numeric_value": 100}
            for p in source.periods for concept in source.metrics.values()]


def test_exact_quarter_excludes_ytd_and_dimensions():
    source = load_recipe("sec_apple_quarterly_income_statement").source
    facts = quarterly_facts(source)
    facts += [{**f, "period_start": f"{date.fromisoformat(f['period_end']).year - 1}-10-01", "numeric_value": 900} for f in facts]
    facts.append({**facts[0], "dimensions": {"segment": "services"}, "numeric_value": 600})
    rows = sec_records(source, facts)
    assert [r["fiscal_quarter"] for r in rows] == [3, 3]
    assert [r["revenue_usd"] for r in rows] == [100, 100]


def test_conflicting_quarter_totals_are_rejected():
    source = load_recipe("sec_apple_quarterly_income_statement").source
    facts = quarterly_facts(source)
    facts.append({**facts[0], "numeric_value": 101})
    with pytest.raises(ValueError, match="unambiguous"):
        sec_records(source, facts)


def test_balance_sheet_requires_exact_instant_context():
    source = load_recipe("sec_apple_balance_sheet").source
    facts = [{"concept": concept, "period_type": "instant", "period_instant": p.end.isoformat(),
              "entity_identifier": "0000320193", "currency": "USD", "numeric_value": 100}
             for p in source.periods for concept in source.metrics.values()]
    facts.append({**facts[0], "period_type": "duration", "period_end": "2023-09-30", "period_start": "2022-09-25", "numeric_value": 800})
    rows = sec_records(source, facts)
    assert rows[0]["assets_usd"] == 100
    assert all("period_start" not in row for row in rows)


@pytest.mark.parametrize("mutation", ["duplicate", "missing_start", "mixed_shape"])
def test_invalid_explicit_periods_fail_before_acquisition(mutation):
    config = load_recipe("sec_apple_quarterly_income_statement").model_dump(mode="json")
    periods = config["source"]["periods"]
    if mutation == "duplicate":
        periods.append(deepcopy(periods[0]))
    elif mutation == "missing_start":
        periods[0]["start"] = None
    else:
        periods[0]["fiscal_quarter"] = None
    with pytest.raises(ValidationError):
        Recipe.model_validate(config)


@pytest.mark.parametrize("product_id", ["sec_apple_quarterly_income_statement", "sec_apple_balance_sheet", "sec_microsoft_cash_flow"])
def test_real_snapshot_replays_deterministically(product_id, tmp_path):
    item = next(p for p in catalog() if p["id"] == product_id)
    directory = local_path(item["source"]["snapshot_metadata"]).parent
    raw = (directory / item["snapshot"]["raw_file"]).read_bytes()
    result = build_snapshot(load_recipe(product_id), raw, retrieved_at=item["snapshot"]["retrieved_at"], root=tmp_path)
    assert result["metadata"]["snapshot_sha256"] == item["snapshot"]["snapshot_sha256"]


def test_expanded_benchmark_balanced_and_evidence_verified():
    cases = expanded.load_cases()
    assert len(cases) == 60
    assert {outcome: sum(c.expected_outcome == outcome for c in cases) for outcome in ("select", "clarify", "abstain")} == {"select": 30, "clarify": 15, "abstain": 15}
    assert sum(c.split == "held_out" for c in cases) == 30
    for split in ("development", "held_out"):
        assert len({c.expected_data_products[0] for c in cases if c.split == split and c.expected_outcome == "select"}) == 15
    assert expanded.validate(cases)["evidence_verified"] == 30


def test_expanded_lock_rejects_metadata_drift():
    products = deepcopy(catalog())
    products[0]["description"] += " changed definition"
    with pytest.raises(ValueError, match="changed"):
        expanded.validate(expanded.load_cases(), products)


def test_full_rank_metrics_and_clarification_not_fake_abstention():
    cases = expanded.load_cases()
    positive = next(c for c in cases if c.expected_outcome == "select")
    unclear = next(c for c in cases if c.expected_outcome == "clarify")
    rows = [{**positive.model_dump(), "rank": 7, "clarification_coverage_at_5": None, "retrieval_ms": 10, "error_type": None},
            {**unclear.model_dump(), "rank": None, "clarification_coverage_at_5": .5, "retrieval_ms": 12, "error_type": None}]
    metrics = expanded.summarize(rows)
    assert metrics["recall_at_5"] == 0
    assert metrics["full_catalog_mrr"] == 1 / 7
    assert metrics["clarification_candidate_coverage_at_5"] == .5
    assert metrics["abstention_accuracy"] is None


def test_provider_errors_are_not_scored_as_abstention(monkeypatch):
    def fail(question):
        raise RuntimeError("private-test-token")
    monkeypatch.setattr(expanded, "rank_all", fail)
    row = expanded.run_case(expanded.load_cases()[0])
    assert "private-test-token" not in json.dumps(row)
    metrics = expanded.summarize([row])
    assert metrics["recall_at_1"] == 0
    assert metrics["error_cases"] == 1
    assert metrics["abstention_accuracy"] is None
