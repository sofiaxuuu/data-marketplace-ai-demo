"""Saved conversations and temporary workflow runs with mocked model calls."""
import os
import shutil
import sqlite3
import uuid
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from datascout import workflows as w, sql_runs, source_advice, external_files
from datascout.catalog import ROOT, product
from datascout.main import app
from datascout.question_context import Interpretation


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "DATABASE", tmp_path / "workflows.sqlite3")
    monkeypatch.setattr(sql_runs, "DATABASE", tmp_path / "sql.sqlite3")
    external_base = ROOT / ".local" / "test-conversations" / tmp_path.name
    monkeypatch.setattr(w, "EXTERNAL_BASE", external_base)
    monkeypatch.setattr(external_files, "BASE", external_base)
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
    yield TestClient(app)
    shutil.rmtree(external_base, ignore_errors=True)


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


def test_uploaded_csv_is_temporary_and_delete_removes_it(client):
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find daily air quality data?")
    run = detail["run"]
    payload = b"date,site,value,unit\n2024-01-01,Seattle,8.2,ug/m3\n2024-01-02,Seattle,9.1,ug/m3\n"
    response = client.post(f"/workflows/{run['id']}/upload", params={"request_id": str(uuid.uuid4()),
        "expected_revision": run["revision"]}, content=payload,
        headers={"content-type": "application/octet-stream", "x-datascout-filename": "daily.csv"})
    assert response.status_code == 200, response.text
    detail = client.get(f"/conversations/{detail['conversation']['id']}").json()
    assert detail["run"]["stage"] == "external_snapshot_review", detail["run"]["error"]
    assert detail["run"]["external_file"]["origin"] == "upload"
    detail = action(client, detail, "confirm_external", confirmed=True)
    assert detail["run"]["stage"] == "ready_for_question"
    detail = ask(client, detail, "Which date had the highest value?")
    detail = action(client, detail, "confirm_interpretation", confirmed=True)
    detail = action(client, detail, "approve_sql", approved=True, plan_id=detail["run"]["plan"]["run_id"])
    assert "propose_registration" not in detail["run"]["allowed_actions"]
    path = external_files.BASE / run["id"] / "snapshot.parquet"
    assert path.exists()
    response = client.delete(f"/conversations/{detail['conversation']['id']}", params={
        "request_id": str(uuid.uuid4()), "expected_revision": detail["conversation"]["revision"]})
    assert response.status_code == 200
    assert not path.exists()


def test_expired_upload_requires_matching_reupload(client):
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find daily air quality data?")
    run = detail["run"]
    payload = b"date,value\n2024-01-01,8.2\n2024-01-02,9.1\n"
    def send_file(run, data):
        return client.post(f"/workflows/{run['id']}/upload", params={"request_id": str(uuid.uuid4()),
            "expected_revision": run["revision"]}, content=data,
            headers={"content-type": "application/octet-stream", "x-datascout-filename": "daily.csv"})
    response = send_file(run, payload)
    assert response.status_code == 200, response.text
    detail = client.get(f"/conversations/{detail['conversation']['id']}").json()
    detail = action(client, detail, "confirm_external", confirmed=True)
    with sqlite3.connect(w.DATABASE) as conn:
        conn.execute("UPDATE workflow_runs SET created = 0 WHERE id = ?", [run["id"]])
        conn.commit()
    detail = client.get(f"/conversations/{detail['conversation']['id']}").json()
    assert detail["run"] is None
    assert not (external_files.BASE / run["id"] / "snapshot.parquet").exists()
    response = client.post(f"/conversations/{detail['conversation']['id']}/resume-upload", json={
        "request_id": str(uuid.uuid4()), "expected_revision": detail["conversation"]["revision"]})
    assert response.status_code == 200, response.text
    detail = client.get(f"/conversations/{detail['conversation']['id']}").json()
    assert detail["run"]["stage"] == "awaiting_upload"
    assert send_file(detail["run"], payload.replace(b"9.1", b"9.2")).status_code == 409
    assert send_file(detail["run"], payload).status_code == 200


def test_upload_rejects_archive_traversal_without_snapshot(client):
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find daily air quality data?")
    run = detail["run"]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.csv", "date,value\n2024-01-01,8.2\n")
    response = client.post(f"/workflows/{run['id']}/upload", params={"request_id": str(uuid.uuid4()),
        "expected_revision": run["revision"]}, content=archive.getvalue(),
        headers={"content-type": "application/octet-stream", "x-datascout-filename": "unsafe.zip"})
    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "upload_error"
    assert "snapshot.parquet" not in [path.name for path in (external_files.BASE / run["id"]).glob("*")]


@pytest.mark.skipif(os.environ.get("DATASCOUT_TEST_EPA_LOCAL") != "1", reason="opt-in large local archive")
def test_uploaded_epa_archive_opt_in(client):
    import duckdb
    from datascout.catalog import ROOT
    path = ROOT / "data" / "daily_88101_2024.zip"
    if not path.is_file():
        pytest.skip("EPA archive not installed")
    detail = create_conversation(client)
    detail = ask(client, detail, "Where can I find daily PM2.5 for Seattle in 2024?")
    run = detail["run"]
    response = client.post(f"/workflows/{run['id']}/upload", params={"request_id": str(uuid.uuid4()),
        "expected_revision": run["revision"]}, content=path.read_bytes(),
        headers={"content-type": "application/octet-stream", "x-datascout-filename": path.name})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["external_file"]["rows"] > 700_000
    assert data["external_file"]["start"] == "2024-01-01"
    item = w.external_item({"run_id": run["id"], "external_file": data["external_file"]})
    sql, _fields = sql_runs.validate_sql(
        "SELECT date_local, arithmetic_mean, units_of_measure FROM external_data "
        "WHERE city_name = 'Seattle' AND site_num = '0030' AND sample_duration = '24-HR BLK AVG' "
        "ORDER BY arithmetic_mean DESC LIMIT 1", item)
    snapshot = external_files.BASE / run["id"] / "snapshot.parquet"
    row = duckdb.connect().execute(sql.replace("external_data", "read_parquet(?)"), [str(snapshot)]).fetchone()
    assert row[0].isoformat() == "2024-12-04"
    assert row[1] == 22.0
    assert row[2] == "Micrograms/cubic meter (LC)"
