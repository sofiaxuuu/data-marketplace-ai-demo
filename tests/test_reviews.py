"""Review persistence, validation and frozen benchmark boundaries."""
import sqlite3

import pytest
from fastapi.testclient import TestClient
from datascout.main import app
from datascout import reviews
from evals import expanded


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "DATABASE", tmp_path / "reviews.sqlite3")
    client = TestClient(app)
    data = client.get("/benchmark-review").json()
    request = {"case_id": data["cases"][0]["id"], "benchmark_hash": data["benchmark_hash"],
               "lock_hash": data["lock_hash"], "decision": "approve"}
    return client, data, request


def test_review_load_and_revision_persistence(setup):
    client, data, request = setup
    before = expanded.BENCHMARK.read_bytes()
    assert len(data["cases"]) == 60 and len(data["products"]) == 15
    assert data["blocked"] is None
    saved = client.post("/benchmark-review", json=request).json()
    assert saved["revision"] == 1 and saved["product_versions"]
    assert client.get("/benchmark-review").json()["reviews"][request["case_id"]]["revision"] == 1
    assert client.post("/benchmark-review", json=request).status_code == 409
    request.update(previous_revision=1, decision="unsure", comment="Needs expertise")
    assert client.post("/benchmark-review", json=request).json()["revision"] == 2
    with sqlite3.connect(reviews.DATABASE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
    assert expanded.BENCHMARK.read_bytes() == before


@pytest.mark.parametrize("patch", [
    {"decision": "correct", "outcome": "select", "product_ids": ["fred_unemployment"]},
    {"decision": "correct", "outcome": "select", "product_ids": [], "comment": "Wrong"},
    {"decision": "correct", "outcome": "clarify", "product_ids": ["fred_unemployment"], "comment": "Unclear"},
    {"decision": "correct", "outcome": "abstain", "product_ids": ["fred_unemployment"], "comment": "Unavailable"},
    {"decision": "correct", "outcome": "select", "product_ids": ["unknown"], "comment": "Wrong"},
    {"decision": "rewrite", "comment": "   "},
    {"product_ids": ["fred_unemployment"]},
])
def test_invalid_review(setup, patch):
    client, _, request = setup
    assert client.post("/benchmark-review", json={**request, **patch}).status_code == 422


@pytest.mark.parametrize("outcome,ids", [("select", ["fred_unemployment_count"]),
    ("clarify", ["fred_unemployment", "fred_underemployment_u6"]), ("abstain", [])])
def test_corrections_are_proposals(setup, outcome, ids):
    client, _, request = setup
    response = client.post("/benchmark-review", json={**request, "decision": "correct", "outcome": outcome,
                                                        "product_ids": ids, "comment": "Business judgment"})
    assert response.status_code == 200
    assert response.json()["product_ids"] == ids


def test_changed_versions_and_catalog_block_saves(setup, monkeypatch):
    client, data, request = setup
    assert client.post("/benchmark-review", json={**request, "benchmark_hash": "old"}).status_code == 409
    assert client.post("/benchmark-review", json=request).status_code == 200
    original = reviews.context
    def drift():
        cases, products, bh, lh, _ = original()
        return cases, products, bh, lh, "Catalog changed"
    monkeypatch.setattr(reviews, "context", drift)
    loaded = client.get("/benchmark-review").json()
    assert loaded["reviews"][request["case_id"]]["stale"]
    assert client.post("/benchmark-review", json={**request, "previous_revision": 1}).status_code == 409


def test_failed_storage_is_sanitized(setup, monkeypatch):
    client, _, request = setup
    def broken():
        raise sqlite3.OperationalError("private path")
    monkeypatch.setattr(reviews, "connection", broken)
    response = client.post("/benchmark-review", json=request)
    assert response.status_code == 503
    assert "private path" not in response.text
