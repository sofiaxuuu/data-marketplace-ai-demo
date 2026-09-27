"""Offline source-file validation and grounded recommendation tests."""
from __future__ import annotations

import io
import zipfile
import uuid

import pytest

from datascout import external_files as files, external_advice, external_registration


CSV = b"Date,Sample Measurement,Units of Measure\n2024-01-01,7.2,Micrograms per cubic meter\n2024-01-02,8.1,Micrograms per cubic meter\n"
URL = "https://example.org/daily_2024.csv"


def test_direct_csv_is_normalized_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "BASE", tmp_path)
    monkeypatch.setattr(files, "fetch", lambda url, limit: CSV)
    meta = files.acquire(str(uuid.uuid4()), URL)
    assert meta["rows"] == 2
    assert meta["start"] == "2024-01-01" and meta["end"] == "2024-01-02"
    assert [c["name"] for c in meta["columns"]] == ["date", "sample_measurement", "units_of_measure"]


def test_zip_with_one_csv_and_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "BASE", tmp_path)
    def archive(name):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr(name, CSV)
        return stream.getvalue()
    monkeypatch.setattr(files, "fetch", lambda url, limit: archive("daily.csv"))
    assert files.acquire(str(uuid.uuid4()), "https://example.org/data.zip")["rows"] == 2
    monkeypatch.setattr(files, "fetch", lambda url, limit: archive("../daily.csv"))
    with pytest.raises(files.ExternalFileError, match="unsafe"):
        files.acquire(str(uuid.uuid4()), "https://example.org/data.zip")
    monkeypatch.setattr(files, "fetch", lambda url, limit: archive("daily.csv"))
    monkeypatch.setattr(files, "EXTRACT_LIMIT", 10)
    with pytest.raises(files.ExternalFileError, match="oversized"):
        files.acquire(str(uuid.uuid4()), "https://example.org/data.zip")


def test_landing_page_proposes_files_without_download(monkeypatch):
    page = b'<a href="/annual_2024.zip">Annual</a><a href="/daily_2024.zip">Daily</a><a href="http://127.0.0.1/private.csv">Unsafe</a>'
    seen = []
    monkeypatch.setattr(files, "fetch", lambda url, limit: (seen.append(url) or page))
    links = files.inspect_page("https://example.org/downloads", "Daily data for 2024")
    assert seen == ["https://example.org/downloads"]
    assert links[0]["name"] == "daily_2024.zip" and len(links) == 2
    assert not files.supported_file("https://127.0.0.1/data.csv")


def test_html_and_missing_date_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "BASE", tmp_path)
    monkeypatch.setattr(files, "fetch", lambda url, limit: b"<html>not a CSV</html>")
    with pytest.raises(files.ExternalFileError, match="HTML"):
        files.acquire(str(uuid.uuid4()), URL)
    monkeypatch.setattr(files, "fetch", lambda url, limit: b"value,place\n7.2,Seattle\n")
    with pytest.raises(files.ExternalFileError, match="date coverage"):
        files.acquire(str(uuid.uuid4()), URL)


def test_external_recommendation_requires_grounded_candidate(monkeypatch):
    candidates = [{"title": "Daily measurements", "publisher": "example.org", "url": URL, "evidence": "Daily PM2.5 for 2024"}]
    assert external_advice.recommend("Question", [])[0]["outcome"] == "insufficient_evidence"
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"outcome":"recommend","primary_index":4,"assessments":[{"candidate_index":4,"fit":"strong","reason":"Daily data","caveat":"Verify units","evidence_quote":"Daily PM2.5 for 2024"}],"unresolved":[]}'}]}]}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return Response()
    monkeypatch.setattr(external_advice.httpx, "Client", Client)
    with pytest.raises(external_advice.ExternalAdviceError, match="invalid evidence"):
        external_advice.recommend("Question", candidates)


def test_reviewed_promotion_uses_existing_snapshot_pipeline(tmp_path, monkeypatch):
    temporary = tmp_path / ".local" / "external-runs"
    monkeypatch.setattr(files, "BASE", temporary)
    monkeypatch.setattr(external_registration, "BASE", temporary)
    monkeypatch.setattr(external_registration, "ROOT", tmp_path)
    monkeypatch.setattr(files, "fetch", lambda url, limit: CSV)
    run_id = str(uuid.uuid4())
    meta = files.acquire(run_id, URL)
    draft = external_registration.proposal(run_id, meta)
    assert draft["coverage"]["rows"] == 2
    review = {"name": "Test PM2.5 data", "description": "Daily monitor readings for a test location.",
        "geography": "Test location", "measure": "PM2.5 concentration", "measure_column": "sample_measurement",
        "unit": "Micrograms per cubic meter", "coverage_column": "date", "unique_key": ["date"]}
    with pytest.raises(files.ExternalFileError, match="unit"):
        external_registration.register(run_id, meta, {**review, "unit": "percent"})
    result = external_registration.register(run_id, meta, review)
    assert result["status"] == "registered"
    assert (tmp_path / "data" / "manifests" / f"{draft['id']}.yaml").is_file()
    assert (tmp_path / "data" / "recipes" / f"{draft['id']}.yaml").is_file()
