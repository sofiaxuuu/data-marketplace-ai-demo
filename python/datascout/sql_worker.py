"""Disposable DuckDB process. Input is trusted by sql_runs, never browser SQL."""
import hashlib
import json
import math
import sys
from datetime import date, datetime
from decimal import Decimal

import duckdb


def value(item):
    if isinstance(item, (date, datetime)):
        return item.isoformat()
    if isinstance(item, Decimal):
        return str(item)
    if isinstance(item, float) and not math.isfinite(item):
        return None
    return item


def main():
    request = json.load(sys.stdin)
    conn = duckdb.connect(config={"memory_limit": "256MB", "threads": "1", "max_temp_directory_size": "0B"})
    try:
        for table in request["tables"]:
            with open(table["path"], "rb") as snapshot:
                if hashlib.file_digest(snapshot, "sha256").hexdigest() != table["sha256"]:
                    raise ValueError("Snapshot changed")
            # Table identifiers have already been strictly validated by the parent.
            conn.execute(f'CREATE TABLE "{table["id"]}" AS SELECT * FROM read_parquet(?)', [table["path"]])
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
        conn.execute("EXPLAIN " + request["sql"])
        if request["validate_only"]:
            print(json.dumps({"validated": True}))
            return
        cursor = conn.execute(request["sql"])
        names = [col[0] for col in cursor.description]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate output names")
        rows = cursor.fetchmany(request["row_limit"] + 1)
        print(json.dumps({"columns": names,
                          "rows": [{name: value(cell) for name, cell in zip(names, row)} for row in rows[:request["row_limit"]]],
                          "truncated": len(rows) > request["row_limit"]}, allow_nan=False))
    except Exception:
        # DuckDB errors can contain private paths or the entire query.
        print(json.dumps({"error": "Query could not be validated or executed against the selected snapshot."}))
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
