from fastapi.testclient import TestClient
import pytest

from datascout.catalog import catalog
from datascout.main import app


client = TestClient(app)
QUESTION = "What was the U.S. unemployment rate in April 2020?"


@pytest.fixture(autouse=True)
def offline_retrieval(monkeypatch):
    monkeypatch.setenv("DATASCOUT_RETRIEVAL_BACKEND", "local")


def test_manifest_matches_real_snapshot():
    products = catalog()
    assert {item["id"] for item in products} == {
        "fred_unemployment",
        "world_bank_us_gdp_per_capita",
        "sec_apple_income_statement",
    }
    fred = next(item for item in products if item["id"] == "fred_unemployment")
    assert fred["snapshot"]["rows"] == 84
    assert fred["tables"][0]["columns"][1]["name"] == "unemployment_rate"


def test_question_requires_confirmation_before_execution():
    proposal = client.post("/runs/preview", json={"question": QUESTION}).json()
    assert proposal["outcome"] == "selected"
    assert proposal["product"]["id"] == "fred_unemployment"
    assert "sql" not in proposal

    response = client.post(
        "/runs/execute",
        json={
            "question": QUESTION,
            "product_id": proposal["product"]["id"],
            "manifest_version": proposal["product"]["version"],
        },
    )
    assert response.status_code == 200
    answer = response.json()
    assert answer["rows"] == [{"observation_date": "2020-04-01", "unemployment_rate": 14.8}]
    assert "FROM unemployment_rate" in answer["sql"]
    assert answer["product"]["snapshot_date"]


def test_no_suitable_source_and_out_of_coverage_abstain():
    for question in (
        "Compare the unemployment rate with GDP in April 2020",
        "What was the U.S. unemployment rate in April 2010?",
        "What was Canada's unemployment rate in April 2020?",
        "What were U.S. jobless claims in April 2020?",
    ):
        response = client.post("/runs/preview", json={"question": question})
        assert response.status_code == 200
        assert response.json()["outcome"] == "abstain"


def test_world_bank_question_uses_its_own_local_snapshot():
    question = "What was U.S. GDP per capita in 2020?"
    proposal = client.post("/runs/preview", json={"question": question}).json()
    assert proposal["product"]["id"] == "world_bank_us_gdp_per_capita"
    response = client.post(
        "/runs/execute",
        json={
            "question": question,
            "product_id": proposal["product"]["id"],
            "manifest_version": proposal["product"]["version"],
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["rows"][0]["year"] == 2020
    assert result["rows"][0]["gdp_per_capita_usd"] > 0
    assert "FROM gdp_per_capita" in result["sql"]


def test_stale_confirmation_is_rejected():
    response = client.post(
        "/runs/execute",
        json={"question": QUESTION, "product_id": "fred_unemployment", "manifest_version": 2},
    )
    assert response.status_code == 400
