"""Expiry cleanup for files left by the retired one-off external workflow."""
from __future__ import annotations

import shutil
import uuid

from .catalog import ROOT


BASE = ROOT / ".local" / "external-runs"


def remove_legacy_files(run_id: str) -> None:
    # Only a server-issued UUID may address one old run directory.
    safe_id = str(uuid.UUID(run_id))
    target = BASE / safe_id
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
