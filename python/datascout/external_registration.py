"""Human-reviewed promotion of a successful one-off CSV snapshot."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .catalog import ROOT, catalog
from .external_files import BASE, ExternalFileError
from .ingestion.models import Recipe
from .ingestion.pipeline import build_snapshot


def proposal(run_id: str, meta: dict) -> dict:
    if meta["start"] == "Unknown" or meta["end"] == "Unknown":
        raise ExternalFileError("Registration requires a verified date column and coverage bounds.")
    url = meta["url"]
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment:
        raise ExternalFileError("This URL cannot be used as a repeatable no-credential recipe.")
    return {"id": "external_" + run_id.replace("-", ""), "version": 1,
        "name": "External CSV from " + (parsed.hostname or "source"),
        "description": "Reviewed one-off CSV snapshot from " + (parsed.hostname or "source"),
        "source_url": url, "retrieved_at": meta["retrieved_at"],
        "coverage": {"start": meta["start"], "end": meta["end"], "rows": meta["rows"]},
        "columns": meta["columns"], "adapter": "csv_zip" if parsed.path.lower().endswith(".zip") else "csv",
        "geography": "", "measure": "", "measure_column": "", "unit": "", "coverage_column": next((c["name"] for c in meta["columns"] if c["type"] == "DATE"), ""),
        "unique_key": []}


def register(run_id: str, meta: dict, review: dict) -> dict:
    draft = proposal(run_id, meta)
    names = {c["name"] for c in meta["columns"]}
    if review["coverage_column"] not in names or review["measure_column"] not in names or not review["unique_key"] or not set(review["unique_key"]) <= names:
        raise ExternalFileError("Select a valid coverage column and primary key from the inspected schema.")
    types_by_name = {c["name"]: c["type"].split("(")[0] for c in meta["columns"]}
    if types_by_name[review["coverage_column"]] != "DATE" or types_by_name[review["measure_column"]] not in ("INTEGER", "BIGINT", "DOUBLE", "FLOAT", "DECIMAL"):
        raise ExternalFileError("Coverage must use a date column and the measure must be numeric.")
    observed = meta.get("units_observed", [])
    if len(observed) > 1 or (len(observed) == 1 and observed[0].casefold() != review["unit"].strip().casefold()):
        raise ExternalFileError("Reviewed unit does not match one consistent observed source unit.")
    if any(p["id"] == draft["id"] for p in catalog()):
        raise ExternalFileError("This source is already registered.")
    for field in ("name", "description", "geography", "measure", "unit"):
        if not review[field].strip():
            raise ExternalFileError(f"Reviewed {field} is required before registration.")
    raw_path = BASE / run_id / "source.csv"
    if not raw_path.is_file():
        raise ExternalFileError("Temporary source bytes expired.")
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["raw_sha256"]:
        raise ExternalFileError("Temporary source bytes changed.")
    types = {"FLOAT": "DOUBLE", "DECIMAL": "DOUBLE"}
    columns = []
    for col in meta["columns"]:
        kind = col["type"].split("(")[0]
        columns.append({"name": col["name"], "type": types.get(kind, kind), "description": col["description"],
                        "paths": [[col["description"]]], "nullable": True,
                        **({"unit": review["unit"]} if col["name"] == review["measure_column"] else {})})
    recipe = Recipe.model_validate({"recipe_version": 1, "id": draft["id"], "version": 1,
        "name": review["name"], "description": review["description"], "business_context": review["description"],
        "concepts": [review["measure"]], "facets": {"geography": review["geography"], "measure": review["measure"],
            "unit": review["unit"], "coverage": f"{meta['start']} through {meta['end']}"},
        "table_id": "external_data", "columns": columns,
        "source_name": urlsplit(meta["url"]).hostname, "source_url": meta["url"],
        "source": {"adapter": draft["adapter"], "url": meta["url"], "allowed_hosts": [urlsplit(meta["url"]).hostname]},
        "unique_key": review["unique_key"], "coverage": {"column": review["coverage_column"],
            "start": meta["start"], "end": meta["end"], "frequency": "bounds"},
        "expected_rows": meta["rows"]})
    recipes = ROOT / "data" / "recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    target = recipes / f"{draft['id']}.yaml"
    fd, staged = tempfile.mkstemp(prefix=".external-recipe-", suffix=".tmp", dir=recipes)
    try:
        with os.fdopen(fd, "w") as handle:
            yaml.safe_dump(recipe.model_dump(mode="json", exclude_none=True), handle, sort_keys=False)
        os.replace(staged, target)
        try:
            result = build_snapshot(recipe, raw, retrieved_at=meta["retrieved_at"][:10], publish=True, root=ROOT)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"product_id": draft["id"], "version": result["manifest_version"], "status": "registered", "indexing": "not_requested"}
    finally:
        Path(staged).unlink(missing_ok=True)
