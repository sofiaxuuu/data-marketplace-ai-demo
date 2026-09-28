"""Durable, user-visible conversation history; no provider calls or graph execution."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL,
            revision INTEGER NOT NULL, title TEXT NOT NULL,
            product_id TEXT, product_version INTEGER, product_fingerprint TEXT,
            active_run_id TEXT, create_key TEXT
        );
        CREATE TABLE IF NOT EXISTS conversation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
            event_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
            created REAL NOT NULL, UNIQUE(conversation_id, event_key)
        );
        CREATE TABLE IF NOT EXISTS conversation_requests (
            conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
            body_hash TEXT NOT NULL, response TEXT,
            PRIMARY KEY(conversation_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS conversation_deletes (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
            expected_revision INTEGER NOT NULL
        );
    """)
    if "create_key" not in {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}:
        conn.execute("ALTER TABLE conversations ADD COLUMN create_key TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS conversations_create_key ON conversations(create_key)")


def create(conn: sqlite3.Connection, title: str, product_id: str | None = None,
           product_version: int | None = None, product_fingerprint: str | None = None,
           create_key: str | None = None) -> dict:
    now = time.time()
    conversation_id = str(uuid.uuid4())
    with conn:
        conn.execute("INSERT INTO conversations (id, created, updated, revision, title, product_id, product_version, "
                     "product_fingerprint, active_run_id, create_key) "
                     "VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL, ?)",
                     [conversation_id, now, now, title[:160], product_id, product_version, product_fingerprint, create_key])
    return get(conn, conversation_id)


def get(conn: sqlite3.Connection, conversation_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", [conversation_id]).fetchone()
    return dict(row) if row else None


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM conversations ORDER BY updated DESC, id DESC").fetchall()]


def events(conn: sqlite3.Connection, conversation_id: str) -> list[dict]:
    return [{"id": row["id"], "event_key": row["event_key"], "kind": row["kind"], "created": row["created"],
             "payload": json.loads(row["payload"])} for row in conn.execute(
        "SELECT * FROM conversation_events WHERE conversation_id = ? ORDER BY id", [conversation_id]).fetchall()]


def put_event(conn: sqlite3.Connection, conversation_id: str, key: str, kind: str, payload: dict) -> None:
    now = time.time()
    serialized = json.dumps(payload, default=str)
    previous = conn.execute("SELECT payload FROM conversation_events WHERE conversation_id = ? AND event_key = ?",
                            [conversation_id, key]).fetchone()
    if previous and previous[0] == serialized:
        return
    with conn:
        conn.execute("INSERT INTO conversation_events (conversation_id, event_key, kind, payload, created) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(conversation_id, event_key) DO UPDATE SET kind=excluded.kind, payload=excluded.payload",
                     [conversation_id, key, kind, serialized, now])
        conn.execute("UPDATE conversations SET updated = ? WHERE id = ?", [now, conversation_id])


def attach_run(conn: sqlite3.Connection, conversation_id: str, run_id: str, title: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE conversations SET active_run_id = ?, revision = revision + 1, "
                     "title = COALESCE(?, title), updated = ? WHERE id = ?",
                     [run_id, title[:160] if title else None, time.time(), conversation_id])


def pin(conn: sqlite3.Connection, conversation_id: str, *, product_id: str | None = None,
        product_version: int | None = None, fingerprint: str | None = None) -> None:
    with conn:
        conn.execute("UPDATE conversations SET product_id = ?, product_version = ?, "
                     "product_fingerprint = ?, updated = ? WHERE id = ?",
                     [product_id, product_version, fingerprint, time.time(), conversation_id])


def detach_run(conn: sqlite3.Connection, conversation_id: str, run_id: str) -> None:
    with conn:
        conn.execute("UPDATE conversations SET active_run_id = NULL WHERE id = ? AND active_run_id = ?",
                     [conversation_id, run_id])
