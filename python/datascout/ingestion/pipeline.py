"""Common normalization, validation, immutable snapshots and publication."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb
import yaml
from filelock import FileLock

from ..catalog import load_manifest
from .adapters import at, records
from .models import ROOT, Column, Recipe

PIPELINE_VERSION = 1


def cast(value, column: Column):
    if value is None:
        if column.nullable:
            return None
        raise ValueError(f"Required column {column.name} is missing")
    if column.type == "VARCHAR":
        return str(value)
    if column.type == "DATE":
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    if isinstance(value, bool):
        raise ValueError("Boolean values cannot be used as numeric observations")
    try:
        number = Decimal(str(value)) * Decimal(str(column.scale))
    except InvalidOperation:
        raise ValueError(f"Invalid numeric value for {column.name}") from None
    if not number.is_finite():
        raise ValueError("Nonfinite numeric observations are not allowed")
    if column.type == "DOUBLE":
        result = float(number)
        if not math.isfinite(result):
            raise ValueError("Numeric observation exceeds DOUBLE range")
        return result
    if number != number.to_integral_value():
        raise ValueError("Fractional observations cannot be cast to integer columns")
    result = int(number)
    bits = 32 if column.type == "INTEGER" else 64
    if not -(2 ** (bits - 1)) <= result < 2 ** (bits - 1):
        raise ValueError("Integer observation exceeds column range")
    return result


def normalize(recipe: Recipe, raw: bytes) -> list[tuple]:
    rows = []
    for record in records(recipe, raw):
        for check in recipe.source.record_checks:
            if at(record, check.path) != check.equals:
                raise ValueError("Source record failed a configured identity assertion")
        if any(at(record, path) is None or str(at(record, path)) in recipe.missing_values for path in recipe.skip_missing):
            continue
        normalized = {}
        for column in recipe.columns:
            paths = [[column.name]] if recipe.source.adapter == "sec_xbrl" else column.paths
            found = False
            for path in paths:
                try:
                    value = at(record, path)
                except ValueError:
                    continue
                found = True
                break
            if not found:
                if not column.nullable:
                    raise ValueError(f"Required source field for {column.name} is missing")
                value = None
            if value is not None and str(value) in recipe.missing_values:
                value = None
            normalized[column.name] = cast(value, column)
        keep = True
        for rule in recipe.filters:
            value = at(normalized, rule.path)
            if isinstance(value, date):
                lower, upper = date.fromisoformat(str(rule.minimum)), date.fromisoformat(str(rule.maximum))
            elif isinstance(value, (int, float)):
                lower, upper = float(rule.minimum), float(rule.maximum)
            else:
                lower, upper = rule.minimum, rule.maximum
            if value is None or not lower <= value <= upper:
                keep = False
        if keep:
            rows.append(tuple(normalized[col.name] for col in recipe.columns))
    validate(recipe, rows)
    indices = [next(i for i, col in enumerate(recipe.columns) if col.name == name) for name in recipe.unique_key]
    return sorted(rows, key=lambda row: tuple(row[i] for i in indices))


def validate(recipe: Recipe, rows: list[tuple]) -> None:
    if not rows:
        raise ValueError("No observations survived normalization")
    if recipe.expected_rows is not None and len(rows) != recipe.expected_rows:
        raise ValueError(f"Expected {recipe.expected_rows} observations; received {len(rows)}")
    names = [col.name for col in recipe.columns]
    keys = [tuple(row[names.index(name)] for name in recipe.unique_key) for row in rows]
    if any(None in key for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("Duplicate or missing primary-key observations")
    coverage = recipe.coverage
    # Coverage bounds refer to normalized values, not raw values before scaling.
    column = recipe.columns[names.index(coverage.column)].model_copy(update={"scale": 1})
    values = [row[names.index(coverage.column)] for row in rows]
    if None in values or min(values) != cast(coverage.start, column) or max(values) != cast(coverage.end, column):
        raise ValueError("Snapshot does not match configured coverage bounds")
    observations = {str(value) for value in values}
    if coverage.frequency == "annual":
        expected = {str(year) for year in range(int(coverage.start), int(coverage.end) + 1)}
    elif coverage.frequency == "monthly":
        start, end = date.fromisoformat(coverage.start), date.fromisoformat(coverage.end)
        if start.day != 1 or end.day != 1:
            raise ValueError("Monthly coverage must use first-of-month dates")
        expected = set()
        current = start
        while current <= end:
            expected.add(current.isoformat())
            current = date(current.year + 1, 1, 1) if current.month == 12 else date(current.year, current.month + 1, 1)
    else:
        return
    if observations != expected:
        raise ValueError("Snapshot has missing or unexpected coverage periods")


def confined(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Snapshot path escapes the configured workspace")
    return path


def build_snapshot(recipe: Recipe, raw: bytes, *, retrieved_at: str,
                   publish: bool = False, root: Path = ROOT) -> dict:
    lock_directory = confined(root, ".cache/ingestion")
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock = confined(root, f".cache/ingestion/{recipe.id}.lock")
    with FileLock(str(lock), timeout=10):
        return _build_snapshot(recipe, raw, retrieved_at=retrieved_at, publish=publish, root=root)


def _build_snapshot(recipe: Recipe, raw: bytes, *, retrieved_at: str,
                   publish: bool = False, root: Path = ROOT) -> dict:
    retrieved_at = date.fromisoformat(retrieved_at).isoformat()
    rows = normalize(recipe, raw)
    source_hash = hashlib.sha256(raw).hexdigest()
    recipe_bytes = json.dumps(recipe.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    recipe_hash = hashlib.sha256(recipe_bytes).hexdigest()
    fingerprint = hashlib.sha256(f"{PIPELINE_VERSION}:{source_hash}:{recipe_hash}:{retrieved_at}".encode()).hexdigest()
    base = confined(root, f"data/snapshots/{recipe.id}")
    base.mkdir(parents=True, exist_ok=True)
    destination = confined(root, f"data/snapshots/{recipe.id}/{fingerprint}")
    parquet_name = f"{recipe.table_id}.parquet"
    raw_name = "source.csv" if recipe.source.adapter == "csv" else "source.json"
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=base) as directory:
        staging = Path(directory)
        parquet = staging / parquet_name
        schema = ", ".join(f'"{col.name}" {col.type}' for col in recipe.columns)
        placeholders = ", ".join("?" for _ in recipe.columns)
        with duckdb.connect() as connection:
            connection.execute(f"CREATE TABLE observations ({schema})")
            connection.executemany(f"INSERT INTO observations VALUES ({placeholders})", rows)
            connection.execute("COPY observations TO ? (FORMAT PARQUET)", [str(parquet)])
        metadata = {
            "pipeline_version": PIPELINE_VERSION, "adapter": recipe.source.adapter,
            "source_url": recipe.source.url or recipe.source_url,
            "retrieved_at": retrieved_at, "source_sha256": source_hash,
            "recipe_sha256": recipe_hash,
            "snapshot_sha256": hashlib.sha256(parquet.read_bytes()).hexdigest(),
            "start": recipe.coverage.start, "end": recipe.coverage.end, "rows": len(rows),
            "raw_file": raw_name,
        }
        if recipe.source.adapter == "sec_xbrl":
            payload = json.loads(raw)
            metadata.update({"cik": recipe.source.cik, "accession": recipe.source.accession,
                             "filing_date": recipe.source.filing_date.isoformat(),
                             "edgartools_version": payload.get("edgartools_version"),
                             "unit": recipe.source.currency, "period_basis": recipe.source.period_basis,
                             "metric_concepts": recipe.source.metrics})
        (staging / raw_name).write_bytes(raw)
        (staging / "recipe.json").write_bytes(recipe_bytes)
        (staging / "snapshot.json").write_text(json.dumps(metadata, indent=2) + "\n")
        if destination.exists():
            # Immutable means never rewrite even an identical snapshot.
            stored = json.loads((destination / "snapshot.json").read_text())
            if stored != metadata or hashlib.sha256((destination / parquet_name).read_bytes()).hexdigest() != metadata["snapshot_sha256"] or hashlib.sha256((destination / raw_name).read_bytes()).hexdigest() != source_hash:
                raise ValueError("Existing immutable snapshot differs or is corrupt")
        else:
            os.rename(staging, destination)

    relative = destination.relative_to(root.resolve()).as_posix()
    manifest = {
        "id": recipe.id, "version": recipe.version, "name": recipe.name,
        "description": recipe.description, "business_context": recipe.business_context,
        "concepts": recipe.concepts,
        "tables": [{"id": recipe.table_id, "path": f"{relative}/{parquet_name}",
                    "columns": [{key: value for key, value in col.model_dump(exclude={"paths", "nullable", "scale"}, exclude_none=True).items()} for col in recipe.columns]}],
        "source": {"name": recipe.source_name, "url": recipe.source_url,
                   "snapshot_metadata": f"{relative}/snapshot.json"},
    }
    result = {"dataset": recipe.id, "state": "validated", "snapshot": relative,
              "metadata": metadata, "manifest_version": recipe.version, "indexing": "not_requested"}
    if publish:
        publish_manifest(manifest, root)
        result.update(state="registered", manifest_version=manifest["version"])
    return result


def publish_manifest(manifest: dict, root: Path) -> None:
    directory = confined(root, "data/manifests")
    directory.mkdir(parents=True, exist_ok=True)
    target = confined(root, f"data/manifests/{manifest['id']}.yaml")
    if target.exists():
        old = yaml.safe_load(target.read_text())
        before, after = dict(old), dict(manifest)
        before.pop("version", None)
        after.pop("version", None)
        manifest["version"] = max(manifest["version"], int(old["version"]) + (before != after))
    # No .yaml suffix: the catalog cannot see a partially written candidate.
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            yaml.safe_dump(manifest, handle, sort_keys=False)
        load_manifest(Path(temporary), root=root)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
