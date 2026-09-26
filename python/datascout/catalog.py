"""Validated manifests for locally queryable data products."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

import duckdb
import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "data/manifests"


class CatalogError(ValueError):
    pass


def local_path(relative: str, root: Path = ROOT) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise CatalogError(f"Path escapes repository: {relative}")
    return path


def load_manifest(path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise CatalogError(f"Invalid manifest: {path.name}")
    for key in ("id", "version", "name", "description", "tables", "source"):
        if not data.get(key):
            raise CatalogError(f"Missing {key} in {path.name}")
    if not isinstance(data["tables"], list):
        raise CatalogError(f"Tables must be a list in {path.name}")

    connection = duckdb.connect()
    try:
        for table in data["tables"]:
            if not isinstance(table, dict) or not table.get("id") or not table.get("path"):
                raise CatalogError(f"Incomplete table in {path.name}")
            parquet = local_path(table["path"], root)
            if not parquet.is_file():
                raise CatalogError(f"Missing snapshot for {data['id']}: {table['path']}")
            actual = {
                row[0]: row[1].upper()
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet)]
                ).fetchall()
            }
            for column in table.get("columns", []):
                if actual.get(column["name"]) != column["type"].upper():
                    raise CatalogError(
                        f"Column mismatch in {data['id']}.{table['id']}: {column['name']}"
                    )
    finally:
        connection.close()

    source_metadata = local_path(data["source"]["snapshot_metadata"], root)
    if not source_metadata.is_file():
        raise CatalogError(f"Missing snapshot metadata for {data['id']}")
    data["snapshot"] = json.loads(source_metadata.read_text())
    if len(data["tables"]) == 1:
        parquet = local_path(data["tables"][0]["path"], root)
        actual_hash = hashlib.sha256(parquet.read_bytes()).hexdigest()
        if actual_hash != data["snapshot"].get("snapshot_sha256"):
            raise CatalogError(f"Snapshot checksum mismatch for {data['id']}")
    return data


def catalog() -> list[dict[str, Any]]:
    return [load_manifest(path) for path in sorted(MANIFESTS.glob("*.yaml"))]


def product(product_id: str) -> dict[str, Any]:
    for item in catalog():
        if item["id"] == product_id:
            return item
    raise CatalogError(f"Unknown product: {product_id}")
