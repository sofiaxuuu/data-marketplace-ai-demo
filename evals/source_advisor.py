"""Development-only source-advisor evaluation. Live calls require --live.

Does not invoke SQL, Exa, checkpoint storage or modify labels/metadata. There is
deliberately no held-out selector: this harness only loads development cases.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from . import expanded
from datascout.catalog import catalog
from datascout.inspection import public_product
from datascout.source_advice import advise, validate_advice


def development_cases():
    return [case for case in expanded.load_cases() if case.split == "development"]


def evaluate(case, metadata):
    expected = {"select": "recommend", "clarify": "clarify", "abstain": "no_local_fit"}[case.expected_outcome]
    row = {"id": case.id, "question": case.question, "expected_outcome": expected,
           "expected_data_products": case.expected_data_products}
    try:
        advice, model, usage = advise(case.question, metadata)
        validated = validate_advice(advice, metadata, [])
        predicted = [r["product_id"] for r in validated["recommendations"]]
        row.update(advice=advice.model_dump(), model=model, usage=usage,
                   outcome_correct=advice.outcome == expected,
                   top_choice_correct=(bool(predicted) and predicted[0] == case.expected_data_products[0]) if case.expected_outcome == "select" else None)
    except Exception as exc:
        row.update(error=type(exc).__name__, outcome_correct=False, top_choice_correct=False if case.expected_outcome == "select" else None)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly permit OpenAI calls")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    products = catalog()
    cases = development_cases()
    expanded.validate(expanded.load_cases(), products)
    if args.limit is not None:
        if args.limit < 1: parser.error("--limit must be positive")
        cases = cases[:args.limit]
    if not args.live:
        print(json.dumps({"split": "development_only", "cases": len(cases), "status": "validated; no provider calls"}))
        return 0
    if not args.output:
        parser.error("--live requires --output under evals/results/")
    output = args.output.resolve()
    if not output.is_relative_to((Path(__file__).parent / "results").resolve()) or output.exists():
        parser.error("Choose a new report filename under evals/results/")
    metadata = [public_product(p) for p in products]
    rows = [evaluate(case, metadata) for case in cases]
    answerable = [r for r in rows if r["top_choice_correct"] is not None]
    report = {"split": "development_only", "created_at": datetime.now(timezone.utc).isoformat(),
              "cases": rows, "outcome_correct": sum(r["outcome_correct"] for r in rows), "total": len(rows),
              "top_choice_correct": sum(r["top_choice_correct"] for r in answerable), "answerable": len(answerable),
              "limitations": "Agent-authored labels; human review pending. Not held-out accuracy, not numerical answer grading; no cost claim."}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as file:
        json.dump(report, file, indent=2)
        file.write("\n")
    print(json.dumps({k: report[k] for k in ("total", "outcome_correct", "answerable", "top_choice_correct")}))
    return int(any("error" in r for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
