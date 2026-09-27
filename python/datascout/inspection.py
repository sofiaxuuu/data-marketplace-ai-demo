"""Public catalog metadata: never expose repository paths or credentials."""


def public_product(item: dict) -> dict:
    snapshot = item["snapshot"]
    return {
        "id": item["id"], "version": item["version"], "name": item["name"],
        "description": item["description"], "business_context": item.get("business_context", ""),
        "facets": item.get("facets", {}),
        "coverage": {"start": snapshot["start"], "end": snapshot["end"], "rows": snapshot["rows"]},
        "snapshot_date": snapshot["retrieved_at"], "source_url": item["source"]["url"],
        "source_name": item["source"]["name"],
        "tables": [{"id": table["id"], "columns": table["columns"]} for table in item["tables"]],
        "execution_supported": True,
        "execution_scope": "Single-product SQL; subject to supported operations and snapshot coverage",
    }
