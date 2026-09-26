"""Public catalog metadata: never expose repository paths or credentials."""

from .analysis import FRED_ID, WORLD_BANK_ID


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
        "execution_supported": item["id"] in (FRED_ID, WORLD_BANK_ID),
        "execution_scope": "Fixed baseline question templates only" if item["id"] in (FRED_ID, WORLD_BANK_ID) else "Inspection only; no execution adapter",
    }
