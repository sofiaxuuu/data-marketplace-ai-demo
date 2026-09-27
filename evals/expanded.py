"""Evaluate full-catalog retrieval, separately from SQL planning and execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from datascout.catalog import catalog, local_path
from datascout.retrieval import content_hash, rank_all

DIRECTORY = Path(__file__).resolve().parent
BENCHMARK = DIRECTORY / "expanded_benchmark.yaml"
LOCK = DIRECTORY / "expanded_catalog_lock.json"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    column: str
    key: dict[str, str | int]


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    split: Literal["development", "held_out"]
    family: str
    question: str = Field(min_length=1)
    expected_outcome: Literal["select", "clarify", "abstain"]
    expected_data_products: list[str]
    label_reason: str
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def consistent(self):
        ids = self.expected_data_products
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate expected products")
        if self.expected_outcome == "select":
            if len(ids) != 1 or not self.evidence or self.evidence.product_id != ids[0]:
                raise ValueError("Answerable labels require one product and snapshot evidence")
        elif self.expected_outcome == "clarify":
            if len(ids) < 2 or self.evidence:
                raise ValueError("Clarification labels require multiple plausible products")
        elif ids or self.evidence:
            raise ValueError("Unavailable labels cannot contain an answer product")
        return self


def load_cases() -> list[Case]:
    payload = yaml.safe_load(BENCHMARK.read_text())
    if payload["benchmark_version"] != 2:
        raise ValueError("Unsupported competing-product benchmark")
    cases = [Case.model_validate(row) for row in payload["cases"]]
    if len({c.id for c in cases}) != len(cases) or len({c.question.casefold() for c in cases}) != len(cases):
        raise ValueError("Duplicate cases")
    families = {}
    for case in cases:
        previous = families.setdefault(case.family, case.split)
        if previous != case.split:
            raise ValueError("Question family leaks across splits")
    return cases


def validate(cases: list[Case], products: list[dict] | None = None) -> dict:
    products = catalog() if products is None else products
    items = {item["id"]: item for item in products}
    locked = json.loads(LOCK.read_text())["products"]
    actual = {item["id"]: {"version": item["version"], "snapshot_sha256": item["snapshot"]["snapshot_sha256"],
                            "metadata_sha256": content_hash(item)} for item in products}
    if actual != locked:
        raise ValueError("Catalog changed; review labels and explicitly re-freeze the expanded benchmark")
    with duckdb.connect() as connection:
        for case in cases:
            if not set(case.expected_data_products) <= items.keys():
                raise ValueError("Label refers to an absent product")
            if not case.evidence:
                continue
            evidence = case.evidence
            table = items[evidence.product_id]["tables"][0]
            columns = {col["name"] for col in table["columns"]}
            names = [evidence.column, *evidence.key]
            if not evidence.key or not set(names) <= columns or any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
                raise ValueError("Invalid snapshot evidence fields")
            where = " AND ".join(f'"{key}" = ?' for key in evidence.key)
            rows = connection.execute(f'SELECT "{evidence.column}" FROM read_parquet(?) WHERE {where} LIMIT 2',
                                      [str(local_path(table["path"])), *evidence.key.values()]).fetchall()
            if len(rows) != 1 or rows[0][0] is None:
                raise ValueError("Evidence does not identify one populated snapshot observation")
    return {"cases": len(cases), "catalog_size": len(products),
            "evidence_verified": sum(c.evidence is not None for c in cases),
            "semantic_labels": "agent-authored; human review pending"}


def run_case(case: Case) -> dict:
    start = time.perf_counter()
    error = None
    try:
        ranking = rank_all(case.question)
    except Exception as exc:
        ranking = []
        error = type(exc).__name__  # Never persist private provider error messages.
    ids = [p["id"] for p in ranking]
    expected = set(case.expected_data_products)
    rank = ids.index(case.expected_data_products[0]) + 1 if case.expected_outcome == "select" and case.expected_data_products[0] in ids else None
    return {**case.model_dump(), "rank": rank,
            "clarification_coverage_at_5": len(expected.intersection(ids[:5])) / len(expected) if case.expected_outcome == "clarify" else None,
            "ranking": [{"id": p["id"], "version": p["version"], "score": p["score"]} for p in ranking],
            "error_type": error, "retrieval_ms": round((time.perf_counter() - start) * 1000, 2)}


def summarize(rows: list[dict]) -> dict:
    positives = [r for r in rows if r["expected_outcome"] == "select"]
    clarification = [r for r in rows if r["expected_outcome"] == "clarify"]
    def mean(values):
        return sum(values) / len(values) if values else None
    latency = sorted(r["retrieval_ms"] for r in rows)
    return {"cases": len(rows), "answerable_cases": len(positives), "clarification_cases": len(clarification),
            "unavailable_cases": sum(r["expected_outcome"] == "abstain" for r in rows),
            **{f"recall_at_{k}": mean([int(r["rank"] is not None and r["rank"] <= k) for r in positives]) for k in (1, 3, 5)},
            "full_catalog_mrr": mean([1 / r["rank"] if r["rank"] else 0 for r in positives]),
            "clarification_candidate_coverage_at_5": mean([r["clarification_coverage_at_5"] for r in clarification]),
            "all_clarification_candidates_at_5": mean([int(r["clarification_coverage_at_5"] == 1) for r in clarification]),
            "error_cases": sum(r["error_type"] is not None for r in rows),
            "retrieval_p95_ms": latency[math.ceil(len(latency) * .95) - 1] if latency else None,
            "abstention_accuracy": None, "automatic_selection_accuracy": None, "numeric_answer_accuracy": None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--split", choices=["development", "held_out", "all"], default="development")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        cases = load_cases()
        validation = validate(cases)
        if args.validate_only:
            print(json.dumps(validation, indent=2))
            return 0
        destination = args.output.resolve() if args.output else None
        if destination and (not destination.is_relative_to((DIRECTORY / "results").resolve()) or destination.exists()):
            raise ValueError("Use a new report filename under evals/results")
        cases = [c for c in cases if args.split == "all" or c.split == args.split]
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(run_case, cases))
        report = {"benchmark_version": 2, "created_at": datetime.now(timezone.utc).isoformat(),
                  "benchmark_sha256": hashlib.sha256(BENCHMARK.read_bytes()).hexdigest(),
                  "catalog_lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest(),
                  "split": args.split, "validation": validation, "metrics": summarize(rows),
                  "by_split": {split: summarize([r for r in rows if r["split"] == split]) for split in ("development", "held_out")},
                  "by_product": {product_id: summarize([r for r in rows if r["expected_outcome"] == "select" and r["expected_data_products"] == [product_id]]) for product_id in json.loads(LOCK.read_text())["products"]},
                  "limitations": ["Agent-authored semantic labels; human review pending", "Retriever ranks candidates; humans clarify and choose",
                                  "Unavailable cases are ranking diagnostics, not measured abstention", "No SQL or numeric-answer quality score",
                                  "Not an official FinSearchComp benchmark", "Provider cost not instrumented; no cost pass claimed"],
                  "cases": rows}
        if destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x") as handle:
                json.dump(report, handle, indent=2)
                handle.write("\n")
        print(json.dumps({"metrics": report["metrics"], "by_split": report["by_split"]}, indent=2))
        return 2 if any(r["error_type"] for r in rows) else 0
    except Exception as exc:
        print(f"Expanded evaluation failed ({type(exc).__name__}); check frozen catalog, evidence, output and provider configuration.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
