"""Development-only model SQL smoke evaluation, independent of retrieval labels.

Run explicitly: PYTHONPATH=python .venv/bin/python -m evals.sql_execution --output PATH
Calls OpenAI. Does not tune or rewrite the retrieval benchmark or catalog lock.
"""
import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import expanded
from datascout.catalog import catalog, product
from datascout.sql_runs import generate, execute, Generate, Execute, fingerprint


# Development-only result labels from the real, pinned local snapshots. Not held-out.
CASES = [
    ("fred_unemployment", "What was the seasonally adjusted U.S. U-3 unemployment percentage in December 2024?", 4.1),
    ("fred_underemployment_u6", "What was the seasonally adjusted U.S. U-6 underutilization percentage in December 2024?", 7.6),
    ("fred_unemployment_unadjusted", "What was the unadjusted U.S. U-3 unemployment percentage in December 2024?", 3.8),
    ("fred_unemployment_count", "How many people were unemployed in the United States in December 2024, seasonally adjusted? Give persons, not thousands.", 6920000),
    ("world_bank_us_gdp_per_capita", "What was U.S. GDP per capita in 2024 in current USD?", 86169.6641581917),
    ("world_bank_us_gdp_total", "What was total U.S. GDP in 2024 in current USD?", 29298013000000),
    ("world_bank_us_gdp_per_capita_real", "What was U.S. GDP per capita in 2024 in constant 2015 USD?", 66856.5131698371),
    ("world_bank_us_gdp_per_capita_ppp", "What was U.S. GDP per capita at PPP in 2024 in current international dollars?", 86169.6641581914),
    ("world_bank_canada_gdp_per_capita", "What was Canadian GDP per capita in 2024 in current USD?", 55015.7066917734),
    ("sec_apple_income_statement", "What was Apple's consolidated revenue in fiscal year 2024? Give whole USD.", 391035000000),
    ("sec_apple_cash_flow", "What was Apple's net operating cash flow in fiscal year 2024? Give whole USD.", 118254000000),
    ("sec_apple_balance_sheet", "What were Apple's total assets at its FY2024 fiscal year end? Give whole USD.", 364980000000),
    ("sec_apple_quarterly_income_statement", "What was Apple's revenue for Q3 FY2024 only, not nine-month YTD? Give whole USD.", 85777000000),
    ("sec_microsoft_income_statement", "What was Microsoft's revenue in fiscal year 2024? Give whole USD.", 245122000000),
    ("sec_microsoft_cash_flow", "What was Microsoft's net financing cash flow in fiscal year 2024? Preserve the sign and give whole USD.", -37757000000),
    ("sec_apple_income_statement", "What was Apple's operating margin in FY2024? Calculate operating income divided by revenue as a percentage without rounding.", 123216000000 * 100 / 391035000000),
    ("sec_apple_income_statement", "How much did Apple's revenue increase from FY2023 to FY2024 in whole USD?", 7750000000),
]
NEGATIVES = [
    ("sec_apple_quarterly_income_statement", "What was Apple's revenue for Q1 FY2024?", "abstain"),
    ("fred_unemployment", "What was the U.S. unemployment percentage in April 2010?", "abstain"),
    ("sec_apple_income_statement", "What was Apple's operating cash flow in FY2024?", "abstain"),
    ("sec_apple_income_statement", "Compare Apple's and Microsoft's revenue in fiscal 2024.", "abstain"),
    ("sec_apple_income_statement", "What was Apple's revenue in 2024? I haven't decided whether to use calendar or fiscal year.", "clarify"),
]


def run_case(case):
    pid, question, expected = case
    item = product(pid)
    row = {"product_id": pid, "question": question, "expected": expected}
    try:
        plan = generate(Generate(question=question, product_id=pid, manifest_version=item["version"], confirmed=True))
        row.update(outcome=plan["outcome"], model=plan["model"], usage=plan.get("usage", {}),
                   sql=plan.get("sql", ""), selected_fields=plan.get("selected_fields", []))
        if isinstance(expected, str):
            row["passed"] = plan["outcome"] == expected
        elif plan["outcome"] != "ready":
            row["passed"] = False
        else:
            result = execute(Execute(run_id=plan["run_id"], approved=True))
            row.update(rows=result["rows"], execution_ms=result["execution_ms"], planning_ms=plan["planning_ms"])
            # Numeric result containment supports model-selected aliases/period labels.
            # This is a smoke check, not a full semantic/gold-table comparison.
            cells = list(result["rows"][0].values()) if len(result["rows"]) == 1 else []
            row["passed"] = any(isinstance(cell, (int, float)) and not isinstance(cell, bool) and
                                math.isclose(cell, expected, rel_tol=1e-8, abs_tol=1e-6) for cell in cells)
    except Exception as exc:
        row.update(passed=False, error_type=type(exc).__name__)
        if hasattr(exc, "status_code"):
            row["http_status"] = exc.status_code
            # Only our sanitized HTTP details are emitted by these endpoints.
            row["error_detail"] = exc.detail
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Do not overwrite an evaluation report.")
    # Reject catalog drift relative to the reviewed metadata and samples.
    products = catalog()
    expanded.validate(expanded.load_cases(), products)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(run_case, [*CASES, *NEGATIVES]))
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "split": "development_only",
              "scope": "Human-selected product; model planning + validated local execution. Numeric containment smoke check, not full semantic accuracy.",
              "products": {p["id"]: fingerprint(p) for p in products}, "cases": rows,
              "passed": sum(r["passed"] for r in rows), "total": len(rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        json.dump(report, file, indent=2)
    print(json.dumps({"passed": report["passed"], "total": report["total"], "report": str(args.output)}))


if __name__ == "__main__":
    main()
