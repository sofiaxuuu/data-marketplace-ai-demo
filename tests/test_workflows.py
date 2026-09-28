"""Real checkpoint/HTTP transitions with mocked specialists; no live providers."""
import json
import sqlite3
import uuid
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi import BackgroundTasks

from datascout import workflows as w, sql_runs, source_advice, discovery
from datascout.catalog import catalog, product
from datascout.inspection import public_product
from datascout.main import app


def advice_for(pid="fred_unemployment", outcome="recommend", limitation="none"):
    return source_advice.Advice(intent="analysis", outcome=outcome, limitation=limitation, reason="Matches fields and snapshot coverage",
        clarification="Which reporting period?" if outcome == "clarify" else "",
        recommendations=[source_advice.Recommendation(product_id=pid, manifest_version=product(pid)["version"], reason="Available measure and period", caveats=["Check reporting basis"])] if outcome == "recommend" else [])


def ready_plan(question, item):
    table = item["tables"][0]
    period = table["columns"][0]["name"]
    measure = next(c["name"] for c in table["columns"] if c.get("unit"))
    return sql_runs.Plan(outcome="ready", reason="Latest snapshot observation",
        sql=f"SELECT {period}, {measure} FROM {table['id']} ORDER BY {period} DESC LIMIT 1",
        selected_fields=[period, measure], formulas=[], assumptions=["Snapshot reporting basis"], result_units=[item["facets"]["unit"]]), "fixture", {"total_tokens": 12}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "DATABASE", tmp_path / "workflows.sqlite3")
    monkeypatch.setattr(sql_runs, "DATABASE", tmp_path / "sql.sqlite3")
    monkeypatch.setattr(w, "advise", lambda *args: (advice_for(), "fixture", {"total_tokens": 10}))
    monkeypatch.setattr(sql_runs, "generate_plan", ready_plan)
    monkeypatch.setattr(sql_runs, "run_worker", lambda item, sql, validate_only=False: {} if validate_only else {
        "columns": ["value"], "rows": [{"value": 4.1}], "truncated": False})
    monkeypatch.setattr(discovery.ExaProvider, "search", lambda self, q: [discovery.Candidate(
        title="Official dataset", publisher="example.org", url="https://example.org/data", evidence="Dataset documentation",
        relevance="Review suitability", provider="exa", discovered_at="2026-09-27T00:00:00Z")])
    monkeypatch.setattr(w, "recommend_external", lambda q, candidates: ({"outcome": "recommend", "answer": "Review the listed dataset.",
        "primary_index": 0, "assessments": [{"candidate_index": 0, "fit": "strong", "reason": "Relevant source", "caveat": "Verify details", "evidence_quote": "Dataset documentation"}],
        "unresolved": []}, "fixture", {}))
    return TestClient(app)


def create(client, pid=None, **changes):
    body = {"request_id": str(uuid.uuid4()), "question": "Latest U.S. unemployment observation", **changes}
    if pid:
        body.update(product_id=pid, manifest_version=product(pid)["version"])
    response = client.post("/workflows", json=body)
    assert response.status_code == 200, response.text
    run = response.json()
    return client.get(f"/workflows/{run['id']}").json()


def action(client, run, kind, **extra):
    return client.post(f"/workflows/{run['id']}/actions", json={"request_id": str(uuid.uuid4()), "expected_revision": run["revision"], "action": {"type": kind, **extra}})


def advance(client, run, kind, **extra):
    response = action(client, run, kind, **extra)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("pid", ["fred_unemployment", "sec_apple_income_statement"])
def test_full_workflow_and_restart_at_each_pause(client, monkeypatch, pid):
    calls = []
    original = sql_runs.generate_plan
    monkeypatch.setattr(sql_runs, "generate_plan", lambda *args: (calls.append("planner") or original(*args)))
    run = create(client, pid)
    assert run["stage"] == "source_review" and run["plan"] is None and calls == []
    # Every GET creates a fresh graph/checkpointer, simulating server re-instantiation.
    assert client.get(f"/workflows/{run['id']}").json()["stage"] == "source_review"
    assert action(client, run, "approve_sql", approved=True, plan_id="wrong").status_code == 409
    run = advance(client, run, "confirm_source", confirmed=True)
    assert run["stage"] == "sql_review" and calls == ["planner"]
    assert client.get(f"/workflows/{run['id']}").json()["plan"] == run["plan"]
    assert calls == ["planner"]
    assert action(client, run, "approve_sql", approved=True, plan_id="wrong").status_code == 409
    run = advance(client, run, "approve_sql", approved=True, plan_id=run["plan"]["run_id"])
    assert run["result"]["rows"] == [{"value": 4.1}] and run["stage"] == "results"
    assert client.get(f"/workflows/{run['id']}").json()["result"] == run["result"]
    assert "data/snapshots/" not in json.dumps(run)
    assert "checkpoints" not in run


def test_full_catalog_advisor_no_retrieval_and_manual_bypass(client, monkeypatch):
    from datascout import retrieval
    monkeypatch.setattr(retrieval, "search", lambda *args: pytest.fail("SingleStore is not required"))
    seen = []
    def advisor(q, metadata, recovery):
        seen.extend(metadata)
        return advice_for("world_bank_canada_gdp_per_capita"), "fixture", {}
    monkeypatch.setattr(w, "advise", advisor)
    run = create(client)
    assert len(seen) == 15 and run["advice"]["recommendations"][0]["product"]["id"] == "world_bank_canada_gdp_per_capita"
    assert "path" not in json.dumps(seen) and "gold" not in json.dumps(seen)
    monkeypatch.setattr(w, "advise", lambda *args: pytest.fail("Manual source selection bypasses advisor"))
    assert create(client, "fred_unemployment")["stage"] == "source_review"


@pytest.mark.parametrize("outcome,limitation", [("clarify", "ambiguous"), ("no_local_fit", "data_gap"), ("no_local_fit", "unsupported_operation")])
def test_distinct_advice_outcomes_and_no_automatic_search(client, monkeypatch, outcome, limitation):
    monkeypatch.setattr(w, "advise", lambda *args: (advice_for(outcome=outcome, limitation=limitation), "fixture", {}))
    monkeypatch.setattr(discovery.ExaProvider, "search", lambda *args: pytest.fail("No automatic search"))
    run = create(client)
    assert run["external"] is None
    assert ("discover_external" in run["allowed_actions"]) == (limitation == "data_gap")


@pytest.mark.parametrize("bad", ["invented", "stale", "duplicate"])
def test_invalid_recommendations_are_failures_not_no_fit(client, monkeypatch, bad):
    advice = advice_for()
    if bad == "invented": advice.recommendations[0].product_id = "not_real"
    if bad == "stale": advice.recommendations[0].manifest_version += 1
    if bad == "duplicate": advice.recommendations.append(advice.recommendations[0])
    monkeypatch.setattr(w, "advise", lambda *args: (advice, "fixture", {}))
    run = create(client)
    assert run["stage"] == "error" and run["advice"] is None and "retry" in run["allowed_actions"]


def test_recovery_context_excludes_rejected_products(client, monkeypatch):
    monkeypatch.setattr(sql_runs, "generate_plan", lambda *args: (sql_runs.Plan(outcome="abstain", reason="Need people counts, not percentages", sql="", selected_fields=[], formulas=[], assumptions=[], result_units=[]), "fixture", {}))
    run = advance(client, create(client, "fred_unemployment"), "confirm_source", confirmed=True)
    assert run["stage"] == "sql_abstain"
    seen = []
    def advisor(q, metadata, recovery):
        seen.append(recovery)
        return advice_for("fred_unemployment_count"), "fixture", {}
    monkeypatch.setattr(w, "advise", advisor)
    run = advance(client, run, "recover_local")
    assert seen[0]["rejected_ids"] == ["fred_unemployment"]
    assert "percentages" in seen[0]["reason"] and run["selected"] is None and run["plan"] is None
    assert run["advice"]["recommendations"][0]["product_id"] == "fred_unemployment_count"
    with pytest.raises(source_advice.AdviceError):
        source_advice.validate_advice(advice_for(), [public_product(p) for p in catalog()], ["fred_unemployment"])


def test_source_switch_clears_approvals_and_saved_plan(client):
    run = advance(client, create(client, "fred_unemployment"), "confirm_source", confirmed=True)
    p = product("sec_apple_income_statement")
    run = advance(client, run, "select_source", product_id=p["id"], manifest_version=p["version"])
    assert run["plan"] is None and run["result"] is None and not run["confirmed"] and not run["approved"]


def test_idempotency_creation_action_and_conflicting_revision(client, monkeypatch):
    key = str(uuid.uuid4())
    run = create(client, "fred_unemployment", request_id=key)
    assert create(client, "fred_unemployment", request_id=key)["id"] == run["id"]
    body = {"request_id": str(uuid.uuid4()), "expected_revision": run["revision"], "action": {"type": "confirm_source", "confirmed": True}}
    first = client.post(f"/workflows/{run['id']}/actions", json=body)
    monkeypatch.setattr(sql_runs, "generate_plan", lambda *args: pytest.fail("Duplicate cannot rerun planner"))
    assert client.post(f"/workflows/{run['id']}/actions", json=body).json() == first.json()
    assert action(client, run, "choose_again").status_code == 409
    body["action"] = {"type": "choose_again"}
    assert client.post(f"/workflows/{run['id']}/actions", json=body).status_code == 409


def test_discovery_requires_consent_and_never_becomes_sql_product(client):
    run = create(client)
    assert action(client, run, "discover_external", consent=True).status_code == 409
    run = advance(client, run, "none_fit")
    assert action(client, run, "discover_external", consent=False).status_code == 422
    run = advance(client, run, "discover_external", consent=True)
    assert run["stage"] == "external_review" and run["selected"] is None and run["plan"] is None
    assert run["external"][0]["coverage"].startswith("Unknown")
    assert client.get(f"/workflows/{run['id']}").json()["external"] == run["external"]
    assert action(client, run, "confirm_source", confirmed=True).status_code == 409


def test_external_recommendation_failure_keeps_candidates_reviewable(client, monkeypatch):
    from datascout.external_advice import ExternalAdviceError
    monkeypatch.setattr(w, "recommend_external", lambda *args: (_ for _ in ()).throw(ExternalAdviceError("advisor failed")))
    run = advance(client, create(client), "none_fit")
    run = advance(client, run, "discover_external", consent=True)
    assert run["stage"] == "error" and len(run["external"]) == 1
    assert "retry" in run["allowed_actions"]
    for removed in ("select_external", "approve_external_file", "confirm_external", "propose_registration", "register_product"):
        assert removed not in run["allowed_actions"]
        assert action(client, run, removed).status_code == 422


def test_failed_step_explicit_retry_and_empty_discovery(client, monkeypatch):
    def fail(*args): raise source_advice.AdviceError("Source advisor unavailable")
    monkeypatch.setattr(w, "advise", fail)
    run = create(client)
    assert run["stage"] == "error"
    monkeypatch.setattr(w, "advise", lambda *args: (advice_for(), "fixture", {}))
    run = advance(client, run, "retry")
    run = advance(client, run, "none_fit")
    monkeypatch.setattr(discovery.ExaProvider, "search", lambda *args: [])
    run = advance(client, run, "discover_external", consent=True)
    assert run["external"] == [] and run["stage"] == "external_review"


def test_catalog_drift_cancellation_and_expiration(client, monkeypatch):
    run = create(client, "fred_unemployment")
    changed = deepcopy(catalog()); changed[0]["description"] += " Updated"
    monkeypatch.setattr(w, "catalog", lambda: changed)
    value = client.get(f"/workflows/{run['id']}").json()
    assert value["status"] == "invalidated" and value["selected"] is None and value["allowed_actions"] == []
    monkeypatch.undo()


def test_cancelled_and_expired_runs_cannot_resume(client):
    run = create(client, "fred_unemployment")
    assert client.delete(f"/workflows/{run['id']}").json()["status"] == "cancelled"
    assert action(client, run, "confirm_source", confirmed=True).status_code == 409
    another = create(client)
    with sqlite3.connect(w.DATABASE) as conn:
        conn.execute("UPDATE workflow_runs SET created = 0 WHERE id = ?", [another["id"]])
    assert client.get(f"/workflows/{another['id']}").status_code == 404
    with sqlite3.connect(w.DATABASE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", [another["id"]]).fetchone()[0] == 0


def test_late_catalog_change_discards_specialist_output(client, monkeypatch):
    original = catalog()
    changed = deepcopy(original); changed[0]["description"] += " drift"
    def advisor(*args):
        monkeypatch.setattr(w, "catalog", lambda: changed)
        return advice_for(), "fixture", {}
    monkeypatch.setattr(w, "advise", advisor)
    run = create(client)
    assert run["status"] == "invalidated" and run["advice"] is None


def test_concurrent_action_and_cancel_during_provider_call(client, monkeypatch):
    run = create(client, "fred_unemployment")
    started, release = Event(), Event()
    def slow(*args):
        started.set(); assert release.wait(5)
        return ready_plan(*args)
    monkeypatch.setattr(sql_runs, "generate_plan", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(action, client, run, "confirm_source", confirmed=True)
        assert started.wait(5)
        assert action(client, run, "choose_again").status_code == 409
        assert client.get(f"/workflows/{run['id']}").json()["status"] == "running"
        assert client.delete(f"/workflows/{run['id']}").json()["status"] == "cancelled"
        release.set()
        assert pending.result().json()["plan"] is None


def test_uncertain_crash_requires_explicit_retry_without_get_calls(client, monkeypatch):
    run = create(client, "fred_unemployment")
    with w.storage() as (conn, saver, graph):
        graph.update_state(w.config(run["id"]), {"confirmed": True}, as_node="dispatch")
        w.mark(conn, run["id"], "running", "planner")
    monkeypatch.setattr(sql_runs, "generate_plan", lambda *args: pytest.fail("GET cannot call planner"))
    value = client.get(f"/workflows/{run['id']}").json()
    assert value["stage"] == "error" and "retry" in value["allowed_actions"]


def test_expired_sql_plan_requires_fresh_analysis(client):
    run = advance(client, create(client, "fred_unemployment"), "confirm_source", confirmed=True)
    with sqlite3.connect(sql_runs.DATABASE) as conn:
        conn.execute("UPDATE sql_runs SET created = 0")
    value = advance(client, run, "approve_sql", approved=True, plan_id=run["plan"]["run_id"])
    assert value["status"] == "invalidated" and "expired" in value["error"] and value["result"] is None


@pytest.mark.parametrize("url", ["http://example.org", "https://127.0.0.1/data", "https://10.0.0.1", "https://localhost", "https://user:password@example.org", "javascript:alert(1)", "https://[::1]", "https://host.internal"])
def test_unsafe_external_links_rejected(url):
    assert not discovery.public_url(url)


def test_exa_request_bounded_evidence_and_no_url_fetch(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    real_client, seen = httpx.Client, []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [{"title": "Official data", "url": f"https://example.org/{i}", "highlights": ["evidence" * 500]} for i in range(8)]})
    monkeypatch.setattr(discovery.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    candidates = discovery.ExaProvider().search("Canada GDP")
    assert len(seen) == 1 and str(seen[0].url) == "https://api.exa.ai/search"
    assert json.loads(seen[0].content)["numResults"] == 5
    assert len(candidates) == 5 and all(len(c.evidence) <= 1200 for c in candidates)


def test_advisor_request_is_metadata_only_and_single_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    real_client, seen = httpx.Client, []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": advice_for().model_dump_json()}]}], "usage": {"total_tokens": 12, "private": "not forwarded"}})
    monkeypatch.setattr(source_advice.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    advice, model, usage = source_advice.advise("U.S. unemployment", [public_product(p) for p in catalog()])
    assert len(seen) == 1 and seen[0]["store"] is False and "tools" not in seen[0]
    assert len(json.loads(seen[0]["input"])["catalog"]) == 15 and usage == {"total_tokens": 12}
    assert advice.outcome == "recommend"


def test_creation_persists_id_before_advisor_and_get_never_starts_it(client, monkeypatch):
    seen = []
    monkeypatch.setattr(w, "advise", lambda *args: (seen.append("advisor") or advice_for(), "fixture", {}))
    tasks = BackgroundTasks()
    run = w.create(w.Create(request_id=uuid.uuid4(), question="U.S. unemployment"), tasks)
    assert run["id"] and run["status"] == "running" and not seen
    assert w.inspect(uuid.UUID(run["id"]))["status"] == "running" and not seen
    asyncio.run(tasks())
    restored = w.inspect(uuid.UUID(run["id"]))
    assert restored["stage"] == "recommendations" and seen == ["advisor"]


def test_duplicate_execution_replays_without_worker(client, monkeypatch):
    run = advance(client, create(client, "fred_unemployment"), "confirm_source", confirmed=True)
    body = {"request_id": str(uuid.uuid4()), "expected_revision": run["revision"],
            "action": {"type": "approve_sql", "approved": True, "plan_id": run["plan"]["run_id"]}}
    first = client.post(f"/workflows/{run['id']}/actions", json=body)
    monkeypatch.setattr(sql_runs, "run_worker", lambda *a, **k: pytest.fail("Duplicate execution"))
    assert client.post(f"/workflows/{run['id']}/actions", json=body).json() == first.json()


def test_missing_provider_keys_are_explicit_without_calls(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("EXA_API_KEY", "")
    with pytest.raises(source_advice.AdviceError, match="OPENAI_API_KEY"):
        source_advice.advise("Question", [])
    with pytest.raises(discovery.DiscoveryError, match="EXA_API_KEY"):
        discovery.ExaProvider().search("Question")


@pytest.mark.parametrize("payload", [{"status": "incomplete"}, {"status": "completed", "output": []},
    {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}])
def test_advisor_incomplete_refusal_or_missing_output_is_failure(monkeypatch, payload):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    real_client = httpx.Client
    monkeypatch.setattr(source_advice.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)), **kwargs))
    with pytest.raises(source_advice.AdviceError):
        source_advice.advise("Question", [])
