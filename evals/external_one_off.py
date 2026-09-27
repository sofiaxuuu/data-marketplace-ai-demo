"""Explicit live evaluation of one approved public CSV/ZIP URL.

This does not search Exa, register a product or index metadata. It downloads the
specified file and calls the configured SQL planner only with --live; execution
requires typing the exact saved plan ID after inspecting the SQL.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

from datascout import sql_runs
from datascout.external_files import acquire, remove, supported_file
from datascout.workflows import external_item


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Permit an external download and model call")
    parser.add_argument("--url", required=True, help="Exact approved direct CSV/ZIP URL")
    parser.add_argument("--question", required=True)
    args = parser.parse_args(argv)
    if not supported_file(args.url):
        parser.error("URL must be a direct public HTTPS CSV/ZIP file")
    if not args.live:
        print("Dry run: add --live to download the file and call the SQL planner.")
        return 0
    if not sys.stdin.isatty():
        parser.error("Live evaluation requires an interactive terminal for SQL approval")
    run_id = str(uuid.uuid4())
    try:
        meta = acquire(run_id, args.url)
        item = external_item({"run_id": run_id, "external_file": meta})
        print(json.dumps({"source": args.url, "coverage": [meta["start"], meta["end"]],
                          "rows": meta["rows"], "columns": meta["columns"], "observed_units": meta["units_observed"]}, indent=2))
        plan = sql_runs.generate_for_item(args.question, item)
        print(json.dumps({"outcome": plan["outcome"], "reason": plan["reason"], "sql": plan["sql"],
                          "assumptions": plan["assumptions"], "result_units": plan["result_units"]}, indent=2))
        if plan["outcome"] != "ready":
            return 1
        if input(f"Type the exact plan ID {plan['run_id']} to execute: ").strip() != plan["run_id"]:
            print("SQL was not approved; nothing executed.")
            return 1
        result = sql_runs.execute_saved_plan(sql_runs.Execute(run_id=plan["run_id"], approved=True))
        print(json.dumps({"outcome": result["outcome"], "rows": result["rows"], "truncated": result["truncated"]}, indent=2))
        return 0
    finally:
        remove(run_id)


if __name__ == "__main__":
    raise SystemExit(main())
