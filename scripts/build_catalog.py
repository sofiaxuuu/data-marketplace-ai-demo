"""Publish missing reviewed products; acquisition requires explicit --download."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from datascout.ingestion.adapters import acquire
from datascout.ingestion.models import ROOT, load_recipe
from datascout.ingestion.pipeline import build_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", required=True)
    args = parser.parse_args()
    failed = []
    for path in sorted((ROOT / "data/recipes").glob("*.yaml")):
        if (ROOT / "data/manifests" / path.name).exists():
            continue  # Never silently refresh an existing snapshot.
        recipe = load_recipe(path.stem)
        try:
            result = build_snapshot(recipe, acquire(recipe), retrieved_at=date.today().isoformat(), publish=True)
            print(json.dumps({"dataset": recipe.id, "state": result["state"], "rows": result["metadata"]["rows"]}), flush=True)
        except Exception as error:
            failed.append(recipe.id)
            # No provider traceback, identity or credentials in console output.
            print(json.dumps({"dataset": recipe.id, "state": "failed", "error_type": type(error).__name__}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
