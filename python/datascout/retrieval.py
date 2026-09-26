"""Manifest ingestion and exact cosine search in SingleStore."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from typing import Any

from .catalog import catalog
from .embeddings import DIMENSIONS, MODEL, EmbeddingError, embed
from .singlestore import ConfigurationError, connect, connection_error_message

TABLE = "datascout_products_v1"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    product_id VARCHAR(128) NOT NULL,
    manifest_version INT NOT NULL,
    name TEXT NOT NULL,
    search_text TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    embedding_model VARCHAR(64) NOT NULL,
    embedding VECTOR({DIMENSIONS}, F32) NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (product_id),
    SHARD KEY (product_id)
)
"""


def document(item: dict[str, Any]) -> str:
    # No local paths or sample rows are sent to the embedding provider.
    lines = [item["name"], item["description"], item.get("business_context", "")]
    lines.append("Concepts: " + ", ".join(item.get("concepts", [])))
    lines.append("Source: " + item["source"]["name"])
    for table in item["tables"]:
        lines.append("Table: " + table["id"])
        for column in table["columns"]:
            lines.append(
                f"{column['name']} ({column['type']}, {column.get('unit', 'no unit')}): "
                + column.get("description", "")
            )
    return "\n".join(line for line in lines if line)


def content_hash(item: dict[str, Any]) -> str:
    return hashlib.sha256(document(item).encode()).hexdigest()


def setup() -> dict[str, object]:
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(f"SELECT COUNT(*) FROM {TABLE}")
            count = cursor.fetchone()[0]
    return {"table": TABLE, "dimensions": DIMENSIONS, "model": MODEL, "products": count}


def ingest() -> dict[str, object]:
    items = catalog()
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            cursor.execute(f"SELECT product_id, manifest_version, content_sha256, embedding_model FROM {TABLE}")
            existing = {row[0]: tuple(row[1:]) for row in cursor.fetchall()}
            pending = [
                item for item in items
                if existing.get(item["id"]) != (item["version"], content_hash(item), MODEL)
            ]
            vectors = embed([document(item) for item in pending]) if pending else []
            for item, vector in zip(pending, vectors, strict=True):
                cursor.execute(
                    f"""INSERT INTO {TABLE}
                    (product_id, manifest_version, name, search_text, content_sha256, embedding_model, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                    manifest_version=VALUES(manifest_version), name=VALUES(name),
                    search_text=VALUES(search_text), content_sha256=VALUES(content_sha256),
                    embedding_model=VALUES(embedding_model), embedding=VALUES(embedding),
                    updated_at=CURRENT_TIMESTAMP""",
                    (item["id"], item["version"], item["name"], document(item), content_hash(item), MODEL, json.dumps(vector)),
                )
    return {"model": MODEL, "ingested": len(pending), "unchanged": len(items) - len(pending)}


def search(question: str, top_k: int = 3) -> list[dict[str, Any]]:
    if not question.strip() or not 1 <= top_k <= 10:
        raise ValueError("Provide a question and top_k between 1 and 10.")
    items = catalog()
    vector = embed([question])[0]
    with connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT product_id, manifest_version, name, content_sha256,
                DOT_PRODUCT(embedding, %s :> VECTOR({DIMENSIONS}, F32)) AS score
                FROM {TABLE} WHERE embedding_model=%s ORDER BY score DESC""",
                (json.dumps(vector), MODEL),
            )
            rows = cursor.fetchall()
    # Ignore deleted/stale manifests instead of returning an unqueryable product.
    valid = {item["id"]: (item["version"], content_hash(item)) for item in items}
    return [
        {"id": row[0], "version": row[1], "name": row[2], "score": float(row[4])}
        for row in rows if valid.get(row[0]) == (row[1], row[3])
    ][:top_k]


def main() -> None:
    parser = argparse.ArgumentParser(description="SingleStore metadata retrieval")
    parser.add_argument("command", choices=["setup", "ingest", "search"])
    parser.add_argument("--question")
    args = parser.parse_args()
    if args.command == "search" and not args.question:
        parser.error("search requires --question")
    try:
        result = setup() if args.command == "setup" else ingest() if args.command == "ingest" else search(args.question)
    except (ConfigurationError, EmbeddingError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as error:
        print(connection_error_message(error), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
