"""Snapshot-grounded retrieval/selection evaluation; no LLM-as-judge."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from datascout.analysis import execute, preview
from datascout.catalog import ROOT, catalog, local_path, load_manifest
from datascout.retrieval import content_hash

DIRECTORY = Path(__file__).resolve().parent


def legacy_catalog() -> list[dict]:
    """The original benchmark retains its original three immutable snapshots."""
    return [load_manifest(path) for path in sorted((DIRECTORY / "legacy_catalog").glob("*.yaml"))]


class Gold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    key: dict[str, str | int]
    value: int | float = Field(allow_inf_nan=False)
    unit: str
    absolute_tolerance: float = Field(ge=0, allow_inf_nan=False)


class Origin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["authored", "finsearchcomp"] = "authored"
    source_id: str | None = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    split: Literal["development", "held_out"]
    category: str
    question: str = Field(min_length=1)
    expected_outcome: Literal["select", "abstain"]
    expected_data_products: list[str]
    label_reason: str
    gold: Gold | None = None
    origin: Origin = Field(default_factory=Origin)

    @model_validator(mode="after")
    def label_is_consistent(self):
        if self.expected_outcome == "select":
            if len(self.expected_data_products) != 1 or not self.gold or self.category != "answerable":
                raise ValueError("Selection cases require one product and numeric gold")
        elif self.expected_data_products or self.gold:
            raise ValueError("Abstention cases have no expected product or numeric answer")
        if self.origin.kind == "finsearchcomp" and not self.origin.source_id:
            raise ValueError("Imported questions require their original source ID")
        return self


def load_cases() -> list[Case]:
    payload = yaml.safe_load((DIRECTORY / "benchmark.yaml").read_text())
    if payload["benchmark_version"] != 1:
        raise ValueError("Unsupported benchmark version")
    cases = [Case.model_validate(row) for row in payload["cases"]]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Duplicate benchmark IDs")
    if len({" ".join(case.question.lower().split()) for case in cases}) != len(cases):
        raise ValueError("Duplicate benchmark questions")
    return cases


def numeric_equal(observed, gold: Gold) -> bool:
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return False
    if isinstance(observed, int) and isinstance(gold.value, int):
        return abs(observed - gold.value) <= gold.absolute_tolerance
    return math.isfinite(observed) and math.isclose(observed, gold.value, rel_tol=0, abs_tol=gold.absolute_tolerance)


def verify_ground_truth(cases: list[Case], products: list[dict] | None = None) -> dict:
    products = legacy_catalog() if products is None else products
    items = {item["id"]: item for item in products}
    locked = json.loads((DIRECTORY / "catalog_lock.json").read_text())["products"]
    for product_id, snapshot_hash in locked.items():
        if product_id not in items or items[product_id]["snapshot"]["snapshot_sha256"] != snapshot_hash:
            raise ValueError("Catalog data changed; review and re-freeze gold labels before evaluating")
    with duckdb.connect() as connection:
        for case in cases:
            if not case.gold:
                continue
            item = items[case.expected_data_products[0]]
            table = item["tables"][0]
            columns = {col["name"]: col for col in table["columns"]}
            gold = case.gold
            if not gold.key or not set([gold.column, *gold.key]) <= set(columns):
                raise ValueError("Gold field is absent from the registered schema")
            if columns[gold.column].get("unit") != gold.unit:
                raise ValueError("Gold unit differs from manifest unit")
            names = [gold.column, *gold.key]
            if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
                raise ValueError("Unsafe gold column identifier")
            # No arbitrary gold SQL: generate a parameterized single-row lookup.
            condition = " AND ".join(f'"{name}" = ?' for name in gold.key)
            rows = connection.execute(
                f'SELECT "{gold.column}" FROM read_parquet(?) WHERE {condition} LIMIT 2',
                [str(local_path(table["path"])), *gold.key.values()],
            ).fetchall()
            if len(rows) != 1 or not numeric_equal(rows[0][0], gold):
                raise ValueError("Frozen numeric gold no longer matches exactly one snapshot observation")
    return {"cases": len(cases), "numeric_gold_verified": sum(case.gold is not None for case in cases),
            "abstention_labels": "policy/coverage annotations; human review pending"}


def score_case(case: Case, proposal: dict, *, use_retrieval: bool) -> dict:
    predicted = proposal.get("product", {}).get("id") if proposal.get("outcome") == "selected" else None
    correct = (proposal.get("outcome") == "selected" and predicted == case.expected_data_products[0]) if case.expected_outcome == "select" else proposal.get("outcome") == "abstain"
    candidates = proposal.get("retrieved_products", [])
    rank = None
    if use_retrieval and case.expected_data_products:
        rank = next((index + 1 for index, item in enumerate(candidates) if item["id"] in case.expected_data_products), None)
    return {
        "id": case.id, "split": case.split, "category": case.category,
        "origin": case.origin.model_dump(), "question": case.question,
        "expected_outcome": case.expected_outcome, "expected_data_products": case.expected_data_products,
        "predicted_outcome": proposal.get("outcome", "error"), "predicted_product": predicted,
        "selection_correct": correct, "expected_product_rank": rank,
        "retrieval_measured": use_retrieval, "retrieved_products": candidates,
        "numeric_correct": None, "execution_attempted": False,
        "errors": [], "failure_categories": [],
    }


def rows_correct(case: Case, answer: dict) -> bool:
    if not case.gold or answer.get("product", {}).get("id") != case.expected_data_products[0]:
        return False
    matching = [row for row in answer.get("rows", []) if all(str(row.get(key)) == str(value) for key, value in case.gold.key.items())]
    return len(matching) == 1 and numeric_equal(matching[0].get(case.gold.column), case.gold)


def run_case(case: Case, *, mode: str, run_execution: bool) -> dict:
    start = time.perf_counter()
    try:
        proposal = preview(case.question, use_retrieval=mode == "singlestore")
    except Exception as exc:
        result = score_case(case, {"outcome": "error"}, use_retrieval=mode == "singlestore")
        result["errors"].append({"stage": "preview", "type": type(exc).__name__})
        result["failure_categories"].append("provider_or_infrastructure_error")
        result["preview_ms"] = round((time.perf_counter() - start) * 1000, 2)
        if case.gold and run_execution:
            result["numeric_correct"] = False
        return result
    result = score_case(case, proposal, use_retrieval=mode == "singlestore")
    result["preview_ms"] = round((time.perf_counter() - start) * 1000, 2)
    result["reason"] = proposal.get("reason")
    result["trace"] = proposal.get("trace", [])
    if not result["selection_correct"]:
        if case.expected_outcome == "abstain":
            result["failure_categories"].append("false_selection")
        elif mode == "singlestore" and result["expected_product_rank"] is None:
            result["failure_categories"].append("retrieval_miss_cause_unattributed")
        else:
            result["failure_categories"].append("selector_failure")
    if run_execution and case.gold:
        result["numeric_correct"] = False
        # Simulated confirmation exists only in this evaluation process. The UI
        # still requires the user to explicitly confirm before executing.
        if proposal.get("outcome") == "selected" and result["selection_correct"]:
            result["execution_attempted"] = True
            started = time.perf_counter()
            try:
                item = proposal["product"]
                answer = execute(case.question, item["id"], item["version"])
                result["numeric_correct"] = rows_correct(case, answer)
                result["sql"] = answer.get("sql")
                result["answer"] = answer.get("answer")
                if not result["numeric_correct"]:
                    result["failure_categories"].append("wrong_numeric_row")
            except Exception as exc:
                result["errors"].append({"stage": "execution", "type": type(exc).__name__})
                result["failure_categories"].append("execution_failure")
            result["execution_ms"] = round((time.perf_counter() - started) * 1000, 2)
        else:
            result["failure_categories"].append("execution_blocked_by_selection")
    return result


def ratio(numerator: int | float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def summarize(results: list[dict]) -> dict:
    answerable = [row for row in results if row["expected_outcome"] == "select"]
    abstentions = [row for row in results if row["expected_outcome"] == "abstain"]
    ranked = [row for row in answerable if row["retrieval_measured"]]
    numeric = [row for row in answerable if row["numeric_correct"] is not None]
    executed = [row for row in numeric if row["execution_attempted"]]
    latencies = sorted(row["preview_ms"] for row in results)
    return {
        "cases": len(results), "answerable_cases": len(answerable), "abstention_cases": len(abstentions),
        "recall_at_1": ratio(sum(row["expected_product_rank"] == 1 for row in ranked), len(ranked)),
        "recall_at_3": ratio(sum(row["expected_product_rank"] is not None for row in ranked), len(ranked)),
        "mrr_at_3": ratio(sum(1 / row["expected_product_rank"] if row["expected_product_rank"] else 0 for row in ranked), len(ranked)),
        "selection_accuracy": ratio(sum(row["selection_correct"] for row in results), len(results)),
        "answerable_selection_accuracy": ratio(sum(row["selection_correct"] for row in answerable), len(answerable)),
        "abstention_accuracy": ratio(sum(row["selection_correct"] for row in abstentions), len(abstentions)),
        "false_selection_rate": ratio(sum(row["predicted_outcome"] == "selected" for row in abstentions), len(abstentions)),
        "numeric_task_accuracy": ratio(sum(row["numeric_correct"] for row in numeric), len(numeric)),
        "returned_row_accuracy": ratio(sum(row["numeric_correct"] for row in executed), len(executed)),
        "execution_attempted": len(executed), "error_cases": sum(bool(row["errors"]) for row in results),
        "preview_p50_ms": latencies[math.ceil(len(latencies) * 0.5) - 1] if latencies else None,
        "preview_p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
    }


def criteria_checks(metrics: dict, criteria: dict) -> dict:
    checks = {}
    for name, bounds in criteria["targets"].items():
        observed = metrics.get(name)
        passed = None if observed is None else all(observed >= value if bound == "minimum" else observed <= value for bound, value in bounds.items())
        checks[name] = {"observed": observed, **bounds, "passed": passed}
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--mode", choices=["local", "singlestore"], default="local")
    parser.add_argument("--split", choices=["development", "held_out", "all"], default="development")
    parser.add_argument("--execute", action="store_true", help="Evaluate numeric rows after simulated confirmation")
    parser.add_argument("--output", type=Path, help="Write a JSON report under evals/results/")
    args = parser.parse_args(argv)
    try:
        products = legacy_catalog()
        if args.mode != "local":
            raise ValueError("Legacy benchmark is local-only; use evals.expanded for the live 15-product index")
        cases = load_cases()
        validation = verify_ground_truth(cases, products)
        if args.validate_only:
            print(json.dumps(validation, indent=2))
            return 0
        if args.output:
            result_path = args.output.resolve()
            if not result_path.is_relative_to((DIRECTORY / "results").resolve()):
                raise ValueError("Reports must stay under evals/results/")
            if result_path.exists():
                raise ValueError("Choose a new output name; benchmark reports are not overwritten")
        cases = [case for case in cases if args.split == "all" or case.split == args.split]
        # Restrict only this evaluation invocation to the original snapshots.
        from unittest.mock import patch
        with patch("datascout.analysis.catalog", return_value=products), patch(
            "datascout.analysis.product", side_effect=lambda product_id: next(item for item in products if item["id"] == product_id)
        ):
            results = [run_case(case, mode=args.mode, run_execution=args.execute) for case in cases]
        splits = {split: summarize([row for row in results if row["split"] == split]) for split in ["development", "held_out"]}
        categories = {category: summarize([row for row in results if row["category"] == category]) for category in sorted({row["category"] for row in results})}
        criteria = json.loads((DIRECTORY / "criteria.json").read_text())
        report = {
            "name": "DataScout snapshot-grounded selection benchmark", "benchmark_version": 1,
            "benchmark_sha256": hashlib.sha256((DIRECTORY / "benchmark.yaml").read_bytes()).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode, "split": args.split,
            "validation": validation, "catalog_size": len(products),
            "catalog": [{"id": item["id"], "version": item["version"], "metadata_sha256": content_hash(item), "snapshot_sha256": item["snapshot"]["snapshot_sha256"]} for item in products],
            "metrics": summarize(results), "by_split": splits, "by_category": categories,
            "by_product": {item["id"]: summarize([row for row in results if row["expected_data_products"] == [item["id"]]]) for item in products},
            "held_out_provisional_checks": criteria_checks(splits["held_out"], criteria),
            "cost": {"provider_usd": None, "status": "not_instrumented; no cost pass claimed"},
            "limitations": ["Agent-authored local labels; human review pending", "Not an official FinSearchComp score", "Recall@3 is diagnostic/trivial with three indexed products", "MRR is truncated at three candidates", "Numeric row correctness does not grade natural-language explanation correctness", "No causal attribution of metadata versus embedding errors without controlled experiments"],
            "cases": results,
        }
        if args.output:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            with result_path.open("x") as handle:
                json.dump(report, handle, indent=2)
                handle.write("\n")
        print(json.dumps({"mode": args.mode, "split": args.split, "metrics": report["metrics"], "by_split": splits, "held_out_provisional_checks": report["held_out_provisional_checks"]}, indent=2))
        return 2 if any(row["errors"] for row in results) else 0
    except Exception as exc:
        # No raw database errors, credentials, request headers or driver traces.
        print(f"Evaluation failed ({type(exc).__name__}). Check frozen snapshot hashes, labels, output path and provider configuration.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
