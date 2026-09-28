"""Opt-in end-to-end check using the user-provided EPA ZIP; no network calls."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import duckdb
import pytest

from datascout import external_files, external_registration


@pytest.mark.skipif(os.environ.get("DATASCOUT_TEST_LOCAL_EPA") != "1", reason="opt-in local EPA integration test")
def test_local_epa_zip_acquisition_and_real_registration(tmp_path, monkeypatch):
    archive = Path(__file__).resolve().parents[1] / "data" / "daily_88101_2024.zip"
    assert archive.is_file(), "Place the EPA ZIP in data/daily_88101_2024.zip"
    source = "https://aqs.epa.gov/aqsweb/airdata/daily_88101_2024.zip"
    temporary = tmp_path / ".local" / "external-runs"
    monkeypatch.setattr(external_files, "BASE", temporary)
    monkeypatch.setattr(external_registration, "BASE", temporary)
    monkeypatch.setattr(external_registration, "ROOT", tmp_path)
    monkeypatch.setattr(external_files, "fetch", lambda url, limit: archive.read_bytes())
    run_id = str(uuid.uuid4())
    meta = external_files.acquire(run_id, source)
    assert meta["rows"] == 740924
    assert (meta["start"], meta["end"]) == ("2024-01-01", "2024-12-31")
    assert meta["units_observed"] == ["Micrograms/cubic meter (LC)"]
    snapshot = temporary / run_id / "snapshot.parquet"
    with duckdb.connect() as connection:
        seattle = connection.execute("SELECT COUNT(*) FROM read_parquet(?) WHERE city_name = 'Seattle'", [str(snapshot)]).fetchone()[0]
    assert seattle > 0
    review = {"name": "EPA 2024 daily PM2.5 monitor observations",
        "description": "U.S. EPA daily PM2.5 monitor observations for 2024, including Seattle sites.",
        "geography": "United States monitoring sites", "measure": "PM2.5 concentration",
        "measure_column": "arithmetic_mean", "unit": "Micrograms/cubic meter (LC)",
        "coverage_column": "date_local", "unique_key": ["state_code", "county_code", "site_num", "poc", "date_local", "sample_duration", "method_code", "event_type"]}
    result = external_registration.register(run_id, meta, review)
    assert result["status"] == "registered"
    assert (tmp_path / "data" / "manifests" / f"{result['product_id']}.yaml").is_file()
    published = list((tmp_path / "data" / "snapshots" / result["product_id"]).glob("*/external_data.parquet"))
    assert len(published) == 1
    with duckdb.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(published[0])]).fetchone()[0] == meta["rows"]
