"""Explicit CLI: inspect recipes, acquire/replay, publish, then optionally index."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from .adapters import acquire, acquire_file
from .models import ROOT, load_recipe
from .pipeline import build_snapshot, file_hash


def input_date(path: Path, raw: bytes | Path, provided: str | None, adapter: str) -> str:
    if provided:
        return date.fromisoformat(provided).isoformat()
    if adapter == "sec_xbrl":
        return date.fromisoformat(json.loads(raw)["retrieved_at"]).isoformat()
    sidecar = path.parent / "snapshot.json"
    if sidecar.is_file():
        metadata = json.loads(sidecar.read_text())
        if metadata.get("source_sha256") == (file_hash(raw) if isinstance(raw, Path) else hashlib.sha256(raw).hexdigest()):
            return date.fromisoformat(metadata["retrieved_at"]).isoformat()
    raise ValueError("Offline input requires --retrieved-at or a matching snapshot sidecar")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List reviewed recipes; no network or writes")
    plan = subparsers.add_parser("plan", help="Inspect a recipe; no network or writes")
    plan.add_argument("dataset")
    build = subparsers.add_parser("build", help="Create a validated local snapshot")
    build.add_argument("dataset")
    inputs = build.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--download", action="store_true", help="Explicitly allow acquisition requests")
    inputs.add_argument("--input", type=Path, help="Replay saved source bytes without network")
    build.add_argument("--retrieved-at", help="Original download date YYYY-MM-DD for offline input")
    build.add_argument("--publish", action="store_true", help="Register a validated manifest atomically")
    build.add_argument("--index", action="store_true", help="After publication, embed/index metadata (requires --publish)")
    args = parser.parse_args(argv)
    if args.command == "build" and args.index and not args.publish:
        parser.error("--index requires --publish")
    try:
        if args.command == "list":
            print(json.dumps([load_recipe(path.stem).id for path in sorted((ROOT / "data/recipes").glob("*.yaml"))], indent=2))
            return 0
        recipe = load_recipe(args.dataset)
        if args.command == "plan":
            print(json.dumps({"state": "configured", "recipe": recipe.model_dump(mode="json"),
                              "requires_explicit_download": True, "execution_adapter": "not_implied_by_registration"}, indent=2))
            return 0
        with tempfile.TemporaryDirectory(prefix="datascout-ingestion-") as directory:
            if args.download:
                if args.retrieved_at:
                    parser.error("--retrieved-at is only for offline replay")
                if recipe.id.startswith("external_") and recipe.source.adapter in ("csv", "csv_zip"):
                    raw = Path(directory) / "source.csv"
                    acquire_file(recipe, raw)
                else:
                    raw = acquire(recipe)
                retrieved_at = date.today().isoformat()
            else:
                raw = args.input if recipe.id.startswith("external_") and recipe.source.adapter in ("csv", "csv_zip") else args.input.read_bytes()
                retrieved_at = input_date(args.input, raw, args.retrieved_at, recipe.source.adapter)
            result = build_snapshot(recipe, raw, retrieved_at=retrieved_at, publish=args.publish)
        if args.index:
            try:
                from ..retrieval import ingest

                result["indexing"] = ingest()
            except Exception as exc:
                result["indexing"] = {"state": "failed", "error_type": type(exc).__name__,
                                      "recovery": "Local publication succeeded. Retry the retrieval ingest command."}
                print(json.dumps(result, indent=2))
                return 2
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        # Raw responses, headers, keys, identity and driver errors stay private.
        print(f"Ingestion failed ({type(exc).__name__}). Check recipe, coverage, source access and original retrieval date; no new manifest was published.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
