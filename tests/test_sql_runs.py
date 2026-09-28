"""Single-product SQL contract, real snapshot results and hostile-query tests."""
import json
import sqlite3
import subprocess
import time
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from datascout import sql_runs as runs
from datascout.catalog import product
from datascout.main import app


GOLD = {
    "fred_underemployment_u6": 7.6, "fred_unemployment": 4.1,
    "fred_unemployment_count": 6920.0, "fred_unemployment_unadjusted": 3.8,
    "sec_apple_balance_sheet": 364980000000, "sec_apple_cash_flow": 118254000000,
    "sec_apple_income_statement": 391035000000, "sec_apple_quarterly_income_statement": 85777000000,
    "sec_microsoft_cash_flow": 118548000000, "sec_microsoft_income_statement": 245122000000,
    "world_bank_canada_gdp_per_capita": 55015.7066917734,
    "world_bank_us_gdp_per_capita": 86169.6641581917,
    "world_bank_us_gdp_per_capita_ppp": 86169.6641581914,
    "world_bank_us_gdp_per_capita_real": 66856.5131698371,
    "world_bank_us_gdp_total": 29298013000000.0,
}


def fixture_plan(item):
    table = item["tables"][0]
    period = table["columns"][0]["name"]
    measure = next(c["name"] for c in table["columns"] if c.get("unit"))
    sql = f'SELECT {period}, {measure} FROM {table["id"]} ORDER BY {period} DESC LIMIT 1'
    return runs.Plan(outcome="ready", reason="Latest observation in the selected local snapshot", sql=sql,
                     selected_fields=[period, measure], formulas=[], assumptions=["Use the snapshot reporting basis"],
                     result_units=[f"{measure}: {item['facets']['unit']}"])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "DATABASE", tmp_path / "sql.sqlite3")
    monkeypatch.setattr(runs, "generate_plan", lambda question, item: (fixture_plan(item), "fixture", {}))
    return TestClient(app)


def generate(client, pid="fred_unemployment", **changes):
    p = product(pid)
    return client.post("/sql-runs/generate", json={"question": "Latest observation", "product_id": pid,
                                                 "manifest_version": p["version"], "confirmed": True, **changes})


@pytest.mark.parametrize("pid,expected", GOLD.items())
def test_all_fifteen_products_execute_real_snapshots(setup, pid, expected):
    response = generate(setup, pid)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["outcome"] == "ready" and "rows" not in plan
    assert "data/snapshots" not in response.text
    result = setup.post("/sql-runs/execute", json={"run_id": plan["run_id"], "approved": True})
    assert result.status_code == 200, result.text
    body = result.json()
    field = next(c["name"] for c in product(pid)["tables"][0]["columns"] if c.get("unit"))
    assert body["rows"][0][field] == pytest.approx(expected)
    assert body["sql"] == plan["sql"]
    assert body["product"]["id"] == pid


@pytest.mark.parametrize("sql", [
    "DROP TABLE unemployment_rate", "SELECT unemployment_rate FROM unemployment_rate; SELECT 1",
    "SELECT unemployment_rate FROM read_parquet('/private/file')",
    "SELECT unemployment_rate FROM unemployment_rate JOIN gdp ON TRUE",
    "SELECT unemployment_rate FROM another_product", "SELECT invented FROM unemployment_rate",
    "SELECT * FROM unemployment_rate", "SELECT getenv('OPENAI_API_KEY') FROM unemployment_rate",
    "SELECT unemployment_rate, random() FROM unemployment_rate",
    "SELECT unemployment_rate FROM information_schema.tables",
    "SELECT unemployment_rate FROM unemployment_rate UNION SELECT 1",
    "WITH x AS (SELECT unemployment_rate FROM unemployment_rate) SELECT * FROM x",
    "SELECT unemployment_rate / 0 FROM unemployment_rate",
    "SELECT unemployment_rate / NULLIF(unemployment_rate, 1) FROM unemployment_rate",
    "SELECT unemployment_rate FROM unemployment_rate LIMIT -1",
    "SELECT unemployment_rate FROM unemployment_rate INTO '/tmp/result'",
    "SELECT (SELECT unemployment_rate FROM unemployment_rate) FROM unemployment_rate",
    "SELECT unemployment_rate, row_number() OVER () FROM unemployment_rate",
])
def test_rejects_unsafe_or_unsupported_sql(sql):
    with pytest.raises(ValueError):
        runs.validate_sql(sql, product("fred_unemployment"))


def test_source_and_sql_approval_required(setup):
    assert generate(setup, confirmed=False).status_code == 422
    assert generate(setup, manifest_version=999).status_code == 409
    plan = generate(setup).json()
    assert setup.post("/sql-runs/execute", json={"run_id": plan["run_id"], "approved": False}).status_code == 422
    assert setup.post("/sql-runs/execute", json={"run_id": "unknown", "approved": True}).status_code == 404
    assert setup.post("/sql-runs/execute", json={"run_id": plan["run_id"], "approved": True, "sql": "SELECT 1"}).status_code == 422


def test_stale_metadata_and_expired_plans(setup, monkeypatch):
    plan = generate(setup).json()
    changed = deepcopy(product("fred_unemployment"))
    changed["description"] += " changed"
    monkeypatch.setattr(runs, "product", lambda pid: changed)
    assert setup.post("/sql-runs/execute", json={"run_id": plan["run_id"], "approved": True}).status_code == 409
    with sqlite3.connect(runs.DATABASE) as conn:
        conn.execute("UPDATE sql_runs SET created = 0")
    assert setup.post("/sql-runs/execute", json={"run_id": plan["run_id"], "approved": True}).status_code == 409


def test_legacy_temporary_plan_is_not_executable(setup):
    plan = generate(setup).json()
    with sqlite3.connect(runs.DATABASE) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM sql_runs WHERE id = ?", [plan["run_id"]]).fetchone()[0])
        payload["temporary_item"] = {"id": "legacy_external"}
        conn.execute("UPDATE sql_runs SET created = ?, payload = ? WHERE id = ?",
                     [time.time(), json.dumps(payload), plan["run_id"]])
    response = setup.post("/sql-runs/execute", json={"run_id": plan["run_id"], "approved": True})
    assert response.status_code == 410


@pytest.mark.parametrize("outcome", ["clarify", "abstain"])
def test_nonready_does_not_execute_or_save_plan(setup, monkeypatch, outcome):
    plan = runs.Plan(outcome=outcome, reason="Clarify reporting basis", sql="", selected_fields=[], formulas=[], assumptions=[], result_units=[])
    monkeypatch.setattr(runs, "generate_plan", lambda q, p: (plan, "fixture", {}))
    monkeypatch.setattr(runs, "run_worker", lambda *a, **k: pytest.fail("Must not run DuckDB"))
    result = generate(setup).json()
    assert result["outcome"] == outcome and "run_id" not in result


def test_invalid_fields_and_provider_failure(setup, monkeypatch):
    plan = fixture_plan(product("fred_unemployment"))
    plan.selected_fields = ["invented"]
    monkeypatch.setattr(runs, "generate_plan", lambda q, p: (plan, "fixture", {}))
    assert generate(setup).status_code == 422
    def fail(q, p):
        raise runs.PlannerError("SQL planner unavailable")
    monkeypatch.setattr(runs, "generate_plan", fail)
    assert generate(setup).status_code == 503


def test_null_denominator_and_income_margin():
    p = product("sec_apple_income_statement")
    sql, fields = runs.validate_sql("SELECT fiscal_year, operating_income_usd * 100.0 / NULLIF(revenue_usd, 0) AS operating_margin_pct FROM annual_income WHERE fiscal_year = 2024", p)
    result = runs.run_worker(p, sql)
    assert result["rows"][0]["operating_margin_pct"] == pytest.approx(123216000000 * 100 / 391035000000)
    assert fields == ["fiscal_year", "operating_income_usd", "revenue_usd"]
    sql, _ = runs.validate_sql("SELECT revenue_usd / NULLIF(0, 0) AS ratio FROM annual_income WHERE fiscal_year = 2024", p)
    assert runs.run_worker(p, sql)["rows"][0]["ratio"] is None


def test_exact_quarter_and_signed_cash_flow():
    p = product("sec_apple_quarterly_income_statement")
    sql, _ = runs.validate_sql("SELECT fiscal_year, fiscal_quarter, period_start, period_end, revenue_usd FROM quarterly_income WHERE fiscal_year = 2024 AND fiscal_quarter = 3", p)
    row = runs.run_worker(p, sql)["rows"][0]
    assert row == {"fiscal_year": 2024, "fiscal_quarter": 3, "period_start": "2024-03-31", "period_end": "2024-06-29", "revenue_usd": 85777000000}
    p = product("sec_microsoft_cash_flow")
    table = p["tables"][0]["id"]
    field = p["tables"][0]["columns"][-1]["name"]
    sql, _ = runs.validate_sql(f"SELECT fiscal_year, {field} FROM {table} WHERE fiscal_year = 2024", p)
    assert runs.run_worker(p, sql)["rows"][0][field] == -37757000000


def test_headcount_conversion_and_conditional_comparison():
    p = product("fred_unemployment_count")
    t = p["tables"][0]["id"]
    sql, _ = runs.validate_sql(f"SELECT observation_date, unemployed_thousands * 1000 AS unemployed_people FROM {t} WHERE observation_date = DATE '2024-12-01'", p)
    assert runs.run_worker(p, sql)["rows"][0]["unemployed_people"] == 6920000
    p = product("sec_apple_income_statement")
    sql, _ = runs.validate_sql("SELECT MAX(CASE WHEN fiscal_year = 2024 THEN revenue_usd END) - MAX(CASE WHEN fiscal_year = 2023 THEN revenue_usd END) AS revenue_change_usd FROM annual_income", p)
    assert runs.run_worker(p, sql)["rows"][0]["revenue_change_usd"] == 7750000000


def test_empty_results_not_zero(setup, monkeypatch):
    plan = fixture_plan(product("fred_unemployment"))
    plan.sql = "SELECT observation_date, unemployment_rate FROM unemployment_rate WHERE observation_date = DATE '2000-01-01'"
    monkeypatch.setattr(runs, "generate_plan", lambda q, p: (plan, "fixture", {}))
    response = generate(setup).json()
    result = setup.post("/sql-runs/execute", json={"run_id": response["run_id"], "approved": True}).json()
    assert result["outcome"] == "no_data" and result["rows"] == []


def test_timeout_and_row_limit(monkeypatch):
    p = product("fred_unemployment")
    sql, _ = runs.validate_sql("SELECT unemployment_rate FROM unemployment_rate LIMIT 99999", p)
    assert "LIMIT 501" in sql
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 5
        assert "OPENAI_API_KEY" not in kwargs["env"]
        raise subprocess.TimeoutExpired("worker", 5)
    monkeypatch.setattr(runs.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="timeout"):
        runs.run_worker(p, sql)


def test_truncation_is_visible_and_duplicate_outputs_fail(monkeypatch):
    p = product("fred_unemployment")
    monkeypatch.setattr(runs, "ROW_LIMIT", 2)
    sql, _ = runs.validate_sql("SELECT observation_date, unemployment_rate FROM unemployment_rate ORDER BY observation_date", p)
    result = runs.run_worker(p, sql)
    assert len(result["rows"]) == 2 and result["truncated"] is True
    sql, _ = runs.validate_sql("SELECT unemployment_rate, unemployment_rate FROM unemployment_rate", p)
    with pytest.raises(ValueError, match="could not be validated"):
        runs.run_worker(p, sql)


def test_hosted_planner_uses_structured_metadata_only(monkeypatch):
    item = product("fred_unemployment")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("DATASCOUT_SQL_MODEL", "test-model")
    response = {"status": "completed", "usage": {"input_tokens": 10}, "output": [{"type": "message", "content": [{"type": "output_text", "text": fixture_plan(item).model_dump_json()}]}]}
    def post(self, url, **kwargs):
        body = kwargs["json"]
        assert body["store"] is False and body["model"] == "test-model"
        assert body["text"]["format"]["strict"] is True
        assert "data/snapshots" not in body["input"] and "test-secret" not in body["input"]
        return httpx.Response(200, json=response, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    plan, model, usage = runs.generate_plan("Latest unemployment", item)
    assert plan.outcome == "ready" and model == "test-model"


def test_planner_refusal_and_incomplete(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    for payload in [{"status": "incomplete"}, {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}]:
        monkeypatch.setattr(httpx.Client, "post", lambda self, url, **k: httpx.Response(200, json=payload, request=httpx.Request("POST", url)))
        with pytest.raises(runs.PlannerError):
            runs.generate_plan("Question", product("fred_unemployment"))
