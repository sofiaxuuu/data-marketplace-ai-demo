"""Saved conversations and temporary workflow runs with mocked model calls."""
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from datascout import workflows as w, sql_runs, source_advice, discovery
from datascout.catalog import product
from datascout.main import app
from datascout.question_context import Interpretation


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "DATABASE", tmp_path / "workflows.sqlite3")
    monkeypatch.setattr(sql_runs, "DATABASE", tmp_path / "sql.sqlite3")
    monkeypatch.setattr(w, "advise", lambda *args: (source_advice.Advice(
        intent="source_finding", outcome="recommend", limitation="none", reason="Relevant coverage",
        clarification="", recommendations=[source_advice.Recommendation(
            product_id="fred_unemployment", manifest_version=product("fred_unemployment")["version"],
            reason="Matching measure", caveats=[])]), "fixture", {}))
    monkeypatch.setattr(w, "interpret", lambda q, h, s: (Interpretation(
        outcome="resolved", question="What was the U.S. unemployment rate in July 2020?", clarification=""), "fixture", {}))
    def plan(question, item):
        table = item["tables"][0]
        date = table["columns"][0]["name"]
        value = next((col["name"] for col in table["columns"] if col.get("unit")), table["columns"][1]["name"])
        return sql_runs.Plan(outcome="ready", reason="Available", sql=f"SELECT {date}, {value} FROM {table['id']} ORDER BY {date} DESC LIMIT 1",
                             selected_fields=[date, value], formulas=[], assumptions=[], result_units=[item["facets"]["unit"]]), "fixture", {}
    monkeypatch.setattr(sql_runs, "generate_plan", plan)
    monkeypatch.setattr(sql_runs, "run_worker", lambda item, sql, validate_only=False: {} if validate_only else {
        "columns": ["value"], "rows": [{"value": 4.1}], "truncated": False})
    return TestClient(app)


def create_conversation(client, pid=None):
    source = product(pid) if pid else None
    response = client.post("/conversations", json={"request_id": str(uuid.uuid4()),
        **({"product_id": source["id"], "manifest_version": source["version"]} if source else {})})
    assert response.status_code == 200, response.text
    return response.json()


def ask(client, detail, question):
    response = client.post(f"/conversations/{detail['conversation']['id']}/turns", json={
        "request_id": str(uuid.uuid4()), "expected_revision": detail["conversation"]["revision"], "question": question})
    assert response.status_code == 200, response.text
    return client.get(f"/conversations/{detail['conversation']['id']}").json()


def action(client, detail, kind, **fields):
    run = detail["run"]
    response = client.post(f"/workflows/{run['id']}/actions", json={
        "request_id": str(uuid.uuid4()), "expected_revision": run["revision"], "action": {"type": kind, **fields}})
    assert response.status_code == 200, response.text
    return client.get(f"/conversations/{detail['conversation']['id']}").json()


def test_catalog_chat_two_approved_turns_and_saved_history(client, monkeypatch):
    detail = create_conversation(client, "fred_unemployment")
    detail = ask(client, detail, "What was unemployment in April 2020?")
    assert detail["run"]["stage"] == "source_review"
    detail = action(client, detail, "confirm_source", confirmed=True)
    assert detail["run"]["stage"] == "sql_review"
    detail = action(client, detail, "approve_sql", approved=True, plan_id=detail["run"]["plan"]["run_id"])
    assert detail["run"]["stage"] == "results"
    detail = ask(client, detail, "What about July?")
    assert detail["run"]["stage"] == "question_review"
    assert detail["run"]["interpreted_question"] == "What was the U.S. unemployment rate in July 2020?"
    assert detail["run"]["plan"] is None
    detail = action(client, detail, "confirm_interpretation", confirmed=True)
    assert detail["run"]["stage"] == "sql_review"
    detail = action(client, detail, "approve_sql", approved=True, plan_id=detail["run"]["plan"]["run_id"])
    assert len([e for e in detail["events"] if e["kind"] == "result"]) == 2
    with sqlite3.connect(w.DATABASE) as conn:
        conn.execute("UPDATE workflow_runs SET created = 0 WHERE id = ?", [detail["run"]["id"]])
        conn.commit()
    restored = client.get(f"/conversations/{detail['conversation']['id']}").json()
    assert restored["run"] is None
    assert len([e for e in restored["events"] if e["kind"] == "result"]) == 2
    new_turn = ask(client, restored, "What was the rate in May 2020?")
    assert new_turn["run"]["stage"] == "source_review"
    assert new_turn["run"]["selected"]["id"] == "fred_unemployment"


def test_duplicate_turn_request_does_not_plan_twice(client, monkeypatch):
    detail = create_conversation(client, "fred_unemployment")
    key = str(uuid.uuid4())
    body = {"request_id": key, "expected_revision": detail["conversation"]["revision"],
            "question": "What was unemployment in April 2020?"}
    first = client.post(f"/conversations/{detail['conversation']['id']}/turns", json=body)
    second = client.post(f"/conversations/{detail['conversation']['id']}/turns", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["run"]["id"] == second.json()["run"]["id"]
    assert len(client.get("/conversations").json()["conversations"]) == 1
    changed = client.post(f"/conversations/{detail['conversation']['id']}/turns", json={**body, "question": "Another question"})
    assert changed.status_code == 409


def test_conversation_creation_and_deletion_are_revision_bound(client):
    key = str(uuid.uuid4())
    first = client.post("/conversations", json={"request_id": key})
    second = client.post("/conversations", json={"request_id": key})
    assert first.status_code == second.status_code == 200
    assert first.json()["conversation"]["id"] == second.json()["conversation"]["id"]
    conversation = first.json()["conversation"]
    endpoint = f"/conversations/{conversation['id']}"
    delete_key = str(uuid.uuid4())
    assert client.delete(endpoint, params={"request_id": delete_key, "expected_revision": 99}).status_code == 409
    args = {"request_id": delete_key, "expected_revision": conversation["revision"]}
    assert client.delete(endpoint, params=args).status_code == 200
    assert client.delete(endpoint, params=args).status_code == 200
    assert client.get(endpoint).status_code == 404


def test_pinned_product_drift_preserves_history_but_blocks_new_turn(client):
    detail = create_conversation(client, "fred_unemployment")
    detail = ask(client, detail, "What was unemployment in April 2020?")
    detail = action(client, detail, "confirm_source", confirmed=True)
    detail = action(client, detail, "approve_sql", approved=True, plan_id=detail["run"]["plan"]["run_id"])
    with sqlite3.connect(w.DATABASE) as conn:
        conn.execute("UPDATE workflow_runs SET created = 0 WHERE id = ?", [detail["run"]["id"]])
        conn.execute("UPDATE conversations SET product_fingerprint = 'stale' WHERE id = ?", [detail["conversation"]["id"]])
        conn.commit()
    restored = client.get(f"/conversations/{detail['conversation']['id']}").json()
    assert len([e for e in restored["events"] if e["kind"] == "result"]) == 1
    response = client.post(f"/conversations/{detail['conversation']['id']}/turns", json={
        "request_id": str(uuid.uuid4()), "expected_revision": restored["conversation"]["revision"],
        "question": "What about July?"})
    assert response.status_code == 409


def test_source_finding_confirmation_does_not_plan_original_question(client):
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find U.S. unemployment data?")
    detail = action(client, detail, "select_source", product_id="fred_unemployment",
                    manifest_version=product("fred_unemployment")["version"])
    detail = action(client, detail, "confirm_source", confirmed=True)
    assert detail["run"]["stage"] == "ready_for_question"
    assert detail["run"]["plan"] is None
    detail = ask(client, detail, "What was the unemployment rate in April 2020?")
    assert detail["run"]["stage"] == "question_review"


def test_source_clarification_revises_question_without_sql(client, monkeypatch):
    calls = []
    def advisor(question, *_args):
        calls.append(question)
        if len(calls) == 1:
            return source_advice.Advice(intent="source_finding", outcome="clarify", limitation="ambiguous",
                reason="Missing place", clarification="Which country?", recommendations=[]), "fixture", {}
        return source_advice.Advice(intent="source_finding", outcome="recommend", limitation="none",
            reason="U.S. coverage", clarification="", recommendations=[source_advice.Recommendation(
                product_id="fred_unemployment", manifest_version=product("fred_unemployment")["version"],
                reason="Rate and period fit", caveats=[])]), "fixture", {}
    monkeypatch.setattr(w, "advise", advisor)
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find unemployment data?")
    assert detail["run"]["stage"] == "clarification"
    detail = action(client, detail, "revise_question", question="Where can I find U.S. unemployment data?")
    assert detail["run"]["stage"] == "recommendations"
    assert detail["run"]["plan"] is None
    assert calls == ["Where can I find unemployment data?", "Where can I find U.S. unemployment data?"]


def test_external_recommendation_is_saved_and_next_question_starts_fresh_run(client, monkeypatch):
    calls = []
    def search(self, question):
        calls.append(("exa", question))
        return [discovery.Candidate(title="Official dataset", publisher="example.org",
            url="https://example.org/data", evidence="Daily observations for 2024",
            relevance="Source review", provider="exa", discovered_at="2026-09-27T00:00:00Z")]
    def recommend(question, candidates):
        calls.append(("advisor", question))
        return ({"outcome": "recommend", "answer": "Investigate the official dataset.",
            "primary_index": 0, "assessments": [{"candidate_index": 0, "fit": "strong",
            "reason": "Matches", "caveat": "Verify units", "evidence_quote": "Daily observations for 2024"}],
            "unresolved": ["Confirm units"]}, "fixture", {})
    monkeypatch.setattr(discovery.ExaProvider, "search", search)
    monkeypatch.setattr(w, "recommend_external", recommend)
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find daily observations for 2024?")
    detail = action(client, detail, "none_fit")
    detail = action(client, detail, "discover_external", consent=True)
    old_id = detail["run"]["id"]
    assert detail["run"]["stage"] == "external_review"
    assert detail["run"]["selected"] is None and detail["run"]["plan"] is None
    assert len([event for event in detail["events"] if event["kind"] == "external_sources"]) == 1
    assert calls == [("exa", "Where can I find daily observations for 2024?"),
                     ("advisor", "Where can I find daily observations for 2024?")]
    client.get(f"/conversations/{detail['conversation']['id']}")
    assert len(calls) == 2
    detail = ask(client, detail, "Where can I find quarterly observations?")
    assert detail["run"]["id"] != old_id
    assert detail["run"]["stage"] == "recommendations"
    assert len(calls) == 2
    assert len([event for event in detail["events"] if event["kind"] == "external_sources"]) == 1


def test_removed_external_file_routes_and_actions(client):
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find daily data?")
    run = detail["run"]
    assert client.post(f"/workflows/{run['id']}/upload", content=b"date,value\\n2024-01-01,1\\n").status_code in (404, 405)
    assert client.post(f"/conversations/{detail['conversation']['id']}/resume-upload", json={}).status_code in (404, 405)
    for kind in ("select_external", "approve_external_file", "confirm_external", "propose_registration", "register_product"):
        response = client.post(f"/workflows/{run['id']}/actions", json={"request_id": str(uuid.uuid4()),
            "expected_revision": run["revision"], "action": {"type": kind}})
        assert response.status_code == 422
