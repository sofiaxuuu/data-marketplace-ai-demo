"""Compatibility command; dataset acquisition is configured in data/recipes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from datascout.ingestion.compat import legacy_main

if __name__ == "__main__":
    legacy_main("fred_unemployment", "--input-csv")
