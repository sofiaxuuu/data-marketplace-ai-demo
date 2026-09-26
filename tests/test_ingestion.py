"""Generic onboarding and publication tests; fixtures never enter the real catalog."""

import json
from datetime import date

import httpx
import pytest
import yaml
from pydantic import ValidationError

from datascout.catalog import CatalogError, load_manifest
from datascout.ingestion import adapters, pipeline
from datascout.ingestion.__main__ import input_date, main
from datascout.ingestion.models import Recipe, load_recipe
from datascout.ingestion.pipeline import build_snapshot, normalize


def csv_config():
    return {
        "recipe_version": 1, "id": "another_provider", "name": "Example observations",
        "description": "A different provider, onboarded without new code",
        "business_context": "Annual observations", "concepts": ["example"],
        "table_id": "observations", "source_name": "Another provider",
        "source_url": "https://data.example.org/observations.csv",
        "source": {"adapter": "csv", "url": "https://data.example.org/observations.csv", "allowed_hosts": ["data.example.org"]},
        "columns": [
            {"name": "year", "type": "INTEGER", "description": "Calendar year", "paths": [["year"]]},
            {"name": "measurement", "type": "DOUBLE", "description": "Measured value", "unit": "USD", "paths": [["amount"]]},
        ],
        "unique_key": ["year"], "coverage": {"column": "year", "start": "2022", "end": "2023", "frequency": "annual"},
        "expected_rows": 2,
    }


def csv_recipe():
    return Recipe.model_validate(csv_config())


RAW = b"year,amount\n2023,20\n2022,10\n"


def test_another_provider_is_configuration_only(tmp_path):
    result = build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", publish=True, root=tmp_path)
    item = load_manifest(tmp_path / "data/manifests/another_provider.yaml", root=tmp_path)
    assert item["snapshot"]["rows"] == 2
    assert result["state"] == "registered"
    assert normalize(csv_recipe(), RAW) == [(2022, 10.0), (2023, 20.0)]


def test_all_initial_datasets_are_recipes():
    assert [load_recipe(name).source.adapter for name in ["fred_unemployment", "world_bank_us_gdp_per_capita", "sec_apple_income_statement"]] == ["csv", "rest_json", "sec_xbrl"]


@pytest.mark.parametrize("raw", [
    b"year,amount\n2022,10\n2022,20\n",  # duplicate key
    b"year,amount\n2022,NaN\n2023,20\n",
    b"year,amount\n2022,Inf\n2023,20\n",
    b"year,amount\n2022,10\n",  # missing period
    b"year,amount\n2022,10\n2024,20\n",  # wrong coverage
    b"year,amount\n2022.5,10\n2023,20\n",  # lossy cast
    b"year,wrong\n2022,10\n2023,20\n",
    b"year,amount,amount\n2022,10,11\n2023,20,21\n",
])
def test_invalid_source_never_publishes(raw, tmp_path):
    with pytest.raises(ValueError):
        build_snapshot(csv_recipe(), raw, retrieved_at="2026-09-25", publish=True, root=tmp_path)
    assert not (tmp_path / "data/manifests").exists()


def test_refresh_is_versioned_and_old_snapshot_is_retained(tmp_path):
    first = build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", publish=True, root=tmp_path)
    repeat = build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", publish=True, root=tmp_path)
    assert repeat["manifest_version"] == first["manifest_version"] == 1
    second = build_snapshot(csv_recipe(), RAW.replace(b",20", b",21"), retrieved_at="2026-09-25", publish=True, root=tmp_path)
    assert second["manifest_version"] == 2
    assert second["snapshot"] != first["snapshot"]
    assert (tmp_path / first["snapshot"] / "observations.parquet").is_file()


def test_failure_before_manifest_replace_preserves_previous_catalog(tmp_path, monkeypatch):
    build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", publish=True, root=tmp_path)
    path = tmp_path / "data/manifests/another_provider.yaml"
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise CatalogError("Rejected candidate")

    monkeypatch.setattr(pipeline, "load_manifest", fail)
    with pytest.raises(CatalogError):
        build_snapshot(csv_recipe(), RAW.replace(b",20", b",21"), retrieved_at="2026-09-25", publish=True, root=tmp_path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_build_does_not_register_without_publish(tmp_path):
    result = build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", root=tmp_path)
    assert result["state"] == "validated"
    assert not (tmp_path / "data/manifests").exists()


def json_recipe():
    config = csv_config()
    config["source"] = {
        "adapter": "rest_json", "url": "https://data.example.org/api",
        "allowed_hosts": ["data.example.org"], "records_path": ["result", "items"],
        "pagination": {"mode": "page", "first_page": 3, "total_pages_path": ["page_count"]},
    }
    return Recipe.model_validate(config)


def test_generic_json_pagination_and_nested_records(monkeypatch):
    monkeypatch.setattr(adapters, "check_public_url", lambda *args: None)
    requested = []

    def handler(request):
        page = int(request.url.params["page"])
        requested.append(page)
        return httpx.Response(200, json={"page_count": 2, "result": {"items": [{"year": 2022 + page - 3, "amount": 10 + page}]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = adapters.acquire(json_recipe(), client)
    assert requested == [3, 4]
    assert normalize(json_recipe(), raw) == [(2022, 13.0), (2023, 14.0)]


def test_partial_saved_paginated_response_is_rejected():
    raw = json.dumps({"page_count": 2, "result": {"items": [{"year": 2022, "amount": 10}]}}).encode()
    with pytest.raises(ValueError, match="missing pages"):
        normalize(json_recipe(), raw)


def test_json_record_identity_is_checked_before_skipping_missing():
    recipe = load_recipe("world_bank_us_gdp_per_capita")
    raw = json.dumps([{"pages": 1}, [{"countryiso3code": "CAN", "indicator": {"id": "NY.GDP.PCAP.CD"}, "value": None}]]).encode()
    with pytest.raises(ValueError, match="identity assertion"):
        normalize(recipe, raw)


def test_headers_use_env_and_response_errors_are_sanitized(monkeypatch, capsys):
    config = csv_config()
    config["source"]["headers_env"] = {"Authorization": "TEST_DATA_TOKEN"}
    recipe = Recipe.model_validate(config)
    monkeypatch.setenv("TEST_DATA_TOKEN", "Bearer private-test-token")
    monkeypatch.setattr(adapters, "check_public_url", lambda *args: None)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-test-token"
        return httpx.Response(403, text="private-test-token")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="HTTP 403") as error:
            adapters.acquire(recipe, client)
    assert "private-test-token" not in str(error.value)


def test_private_network_is_rejected(monkeypatch):
    monkeypatch.setattr(adapters.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="Private"):
        adapters.check_public_url("https://data.example.org/api", ["data.example.org"])


@pytest.mark.parametrize("change", [
    {"id": "../../escape"},
    {"source": {"adapter": "csv", "url": "http://data.example.org/api", "allowed_hosts": ["data.example.org"]}},
    {"source": {"adapter": "csv", "url": "https://data.example.org/api", "allowed_hosts": []}},
    {"source": {"adapter": "csv", "url": "https://data.example.org/api", "allowed_hosts": ["data.example.org"], "params": {"api_key": "secret"}}},
    {"execute_python": "print('no')"},
])
def test_untrusted_configuration_is_rejected(change):
    with pytest.raises(ValidationError):
        Recipe.model_validate({**csv_config(), **change})


def test_plan_and_list_do_not_download(monkeypatch, capsys):
    import datascout.ingestion.__main__ as cli

    monkeypatch.setattr(cli, "acquire", lambda *args: pytest.fail("Unexpected acquisition"))
    assert main(["list"]) == 0
    assert main(["plan", "fred_unemployment"]) == 0
    assert "configured" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["build", "fred_unemployment"])


def test_offline_date_is_not_fabricated(tmp_path):
    path = tmp_path / "source.csv"
    with pytest.raises(ValueError, match="retrieved-at"):
        input_date(path, RAW, None, "csv")
    assert input_date(path, RAW, "2024-01-01", "csv") == "2024-01-01"


def test_csv_alternate_headers_and_complete_monthly_coverage():
    recipe = load_recipe("fred_unemployment")
    lines = ["DATE,UNRATE"]
    for year in range(2018, 2025):
        lines.extend(f"{year}-{month:02d}-01,4.5" for month in range(1, 13))
    raw = ("\n".join(lines) + "\n").encode()
    assert len(normalize(recipe, raw)) == 84
    assert normalize(recipe, raw)[0] == (date(2018, 1, 1), 4.5)


def test_numeric_scaling_is_explicit():
    config = csv_config()
    config["columns"][1]["scale"] = 1000000
    assert normalize(Recipe.model_validate(config), RAW) == [(2022, 10000000.0), (2023, 20000000.0)]


def test_json_preserves_large_integer_decimal_values(monkeypatch):
    config = csv_config()
    config["columns"][1]["type"] = "BIGINT"
    config["source"] = {"adapter": "rest_json", "url": "https://data.example.org/api", "allowed_hosts": ["data.example.org"], "records_path": ["rows"]}
    recipe = Recipe.model_validate(config)
    raw = b'{"rows":[{"year":2022,"amount":9007199254740993.0},{"year":2023,"amount":9007199254740995.0}]}'
    monkeypatch.setattr(adapters, "check_public_url", lambda *args: None)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))) as client:
        saved = adapters.acquire(recipe, client)
    assert normalize(recipe, saved) == [(2022, 9007199254740993), (2023, 9007199254740995)]


def test_failed_indexing_does_not_discard_local_publication(tmp_path, monkeypatch, capsys):
    import datascout.ingestion.__main__ as cli
    import datascout.retrieval as retrieval

    path = tmp_path / "input.csv"
    # Fixture setup uses a local test file, never the production catalog.
    path.write_bytes(RAW)
    monkeypatch.setattr(cli, "load_recipe", lambda dataset: csv_recipe())
    monkeypatch.setattr(cli, "build_snapshot", lambda recipe, raw, **kwargs: build_snapshot(recipe, raw, **kwargs, root=tmp_path))

    def fail():
        raise RuntimeError("private-test-token")

    monkeypatch.setattr(retrieval, "ingest", fail)
    assert main(["build", "another_provider", "--input", str(path), "--retrieved-at", "2026-09-25", "--publish", "--index"]) == 2
    output = capsys.readouterr().out
    assert "private-test-token" not in output
    assert json.loads(output)["indexing"]["state"] == "failed"
    assert load_manifest(tmp_path / "data/manifests/another_provider.yaml", root=tmp_path)["snapshot"]["rows"] == 2


def test_private_path_symlink_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "data").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", root=root)


def test_existing_snapshot_tampering_is_not_overwritten(tmp_path):
    result = build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", root=tmp_path)
    raw_path = tmp_path / result["snapshot"] / "source.csv"
    raw_path.write_bytes(b"tampered fixture")
    with pytest.raises(ValueError, match="corrupt"):
        build_snapshot(csv_recipe(), RAW, retrieved_at="2026-09-25", root=tmp_path)
    assert raw_path.read_bytes() == b"tampered fixture"
