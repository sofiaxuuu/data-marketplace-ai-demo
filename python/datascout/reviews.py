"""Local, append-only benchmark review proposals; never promote labels."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from evals import expanded
from .catalog import ROOT, catalog, CatalogError
from .inspection import public_product

router = APIRouter(prefix="/benchmark-review")
DATABASE = ROOT / ".local" / "benchmark-reviews.sqlite3"


def connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
        revision INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
        benchmark_hash TEXT NOT NULL, lock_hash TEXT NOT NULL, payload TEXT NOT NULL)""")
    return conn


def context():
    cases = expanded.load_cases()
    products = catalog()
    benchmark_hash = hashlib.sha256(expanded.BENCHMARK.read_bytes()).hexdigest()
    lock_hash = hashlib.sha256(expanded.LOCK.read_bytes()).hexdigest()
    blocked = None
    try:
        expanded.validate(cases, products)
    except ValueError:
        blocked = "Catalog or evidence changed. Re-freeze the benchmark before saving reviews."
    return cases, products, benchmark_hash, lock_hash, blocked


def latest():
    conn = connection()
    try:
        rows = conn.execute("SELECT revision, case_id, benchmark_hash, lock_hash, payload FROM reviews ORDER BY revision").fetchall()
    finally:
        conn.close()
    return {row[1]: {**json.loads(row[4]), "revision": row[0], "benchmark_hash": row[2], "lock_hash": row[3]} for row in rows}


@router.get("")
def load_review():
    try:
        cases, products, bh, lh, blocked = context()
        reviews = latest()
        for review in reviews.values():
            review["stale"] = bool(blocked or review["benchmark_hash"] != bh or review["lock_hash"] != lh)
        return {"cases": [c.model_dump() for c in cases], "products": [public_product(p) for p in products],
                "reviews": reviews, "benchmark_hash": bh, "lock_hash": lh, "blocked": blocked}
    except (ValueError, CatalogError, OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "Benchmark review unavailable; check local files and storage.") from exc


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    benchmark_hash: str
    lock_hash: str
    previous_revision: int | None = None
    decision: Literal["approve", "correct", "rewrite", "unsure"]
    outcome: Literal["select", "clarify", "abstain"] | None = None
    product_ids: list[str] = Field(default_factory=list, max_length=15)
    comment: str = Field(default="", max_length=4000)


@router.post("")
def save_review(request: Review):
    try:
        cases, products, bh, lh, blocked = context()
        if blocked or request.benchmark_hash != bh or request.lock_hash != lh:
            raise HTTPException(409, blocked or "Benchmark changed. Reload before reviewing.")
        case = next((c for c in cases if c.id == request.case_id), None)
        if case is None:
            raise HTTPException(404, "Unknown benchmark case")
        ids = request.product_ids
        if len(set(ids)) != len(ids) or not set(ids) <= {p["id"] for p in products}:
            raise HTTPException(422, "Choose distinct products from the current catalog.")
        if request.decision in ("correct", "rewrite") and not request.comment.strip():
            raise HTTPException(422, "Explain the proposed correction or rewrite.")
        if request.decision == "correct":
            if not ((request.outcome == "select" and len(ids) == 1) or
                    (request.outcome == "clarify" and len(ids) >= 2) or
                    (request.outcome == "abstain" and not ids)):
                raise HTTPException(422, "Choose one product, multiple clarification candidates, or no suitable product.")
        elif request.outcome is not None or ids:
            raise HTTPException(422, "Only corrections accept an alternative outcome or products.")
        payload = request.model_dump(exclude={"benchmark_hash", "lock_hash", "previous_revision"})
        referenced = ids if request.decision == "correct" else case.expected_data_products
        payload.update(saved_at=datetime.now(timezone.utc).isoformat(),
                       product_versions={p["id"]: p["version"] for p in products if p["id"] in referenced})
        conn = connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT MAX(revision) FROM reviews WHERE case_id = ?", [case.id]).fetchone()[0]
            if previous != request.previous_revision:
                raise HTTPException(409, "Another review was saved. Reload to see the latest decision.")
            cursor = conn.execute("INSERT INTO reviews(case_id, benchmark_hash, lock_hash, payload) VALUES (?, ?, ?, ?)",
                                  [case.id, bh, lh, json.dumps(payload)])
            conn.commit()
            return {**payload, "revision": cursor.lastrowid, "benchmark_hash": bh, "lock_hash": lh, "stale": False}
        finally:
            conn.close()
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise HTTPException(503, "Could not save review; local review storage or benchmark unavailable.") from exc
