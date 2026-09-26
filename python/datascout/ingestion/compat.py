"""Old snapshot commands delegate to the same shared publisher."""

import argparse
from pathlib import Path

from .__main__ import main


def legacy_main(dataset: str, input_flag: str) -> None:
    parser = argparse.ArgumentParser(description="Compatibility wrapper for datascout.ingestion")
    parser.add_argument(input_flag, type=Path, dest="input")
    parser.add_argument("--retrieved-at", help="Original source download date for offline files")
    args = parser.parse_args()
    argv = ["build", dataset, "--publish"]
    if args.input:
        argv += ["--input", str(args.input)]
        if args.retrieved_at:
            argv += ["--retrieved-at", args.retrieved_at]
    else:
        argv += ["--download"]
    raise SystemExit(main(argv))
