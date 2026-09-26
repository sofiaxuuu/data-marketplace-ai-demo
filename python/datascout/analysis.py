"""Conservative, deterministic selection for the initial local product catalog."""

from __future__ import annotations

import calendar
import re
import time
from datetime import date
from typing import Any

import duckdb

from .catalog import catalog, local_path, product


FRED_ID = "fred_unemployment"
WORLD_BANK_ID = "world_bank_us_gdp_per_capita"
UNEMPLOYMENT_RATE = re.compile(
    r"\bunemployment rate\b|\brate of unemployment\b", re.IGNORECASE
)
GDP_PER_CAPITA = re.compile(
    r"\bgdp per capita\b|\beconomic output per person\b", re.IGNORECASE
)
UNSUPPORTED = re.compile(
    r"\b(inflation|cpi|sec|revenue|net income|apple|microsoft|jobless claims)\b",
    re.IGNORECASE,
)
UNITED_STATES = re.compile(r"\b(us|usa|united states)\b", re.IGNORECASE)
YEAR = re.compile(r"\b(20\d\d)\b")


def months_in(question: str) -> list[int]:
    return [
        number
        for number in range(1, 13)
        if re.search(
            rf"\b{calendar.month_name[number]}\b|\b{calendar.month_abbr[number]}\b",
            question,
            re.I,
        )
    ]


def abstain(reason: str, stage: str, start: float) -> dict[str, Any]:
    return {
        "outcome": "abstain",
        "reason": reason,
        "candidates": [{"id": item["id"], "name": item["name"]} for item in catalog()],
        "trace": [{"stage": stage, "result": reason}],
        "latency_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def _preview_local(question: str) -> dict[str, Any]:
    start = time.perf_counter()
    question = question.strip()
    if not question:
        raise ValueError("Enter a question.")

    geography = question.replace(".", "")
    wants_fred = bool(UNEMPLOYMENT_RATE.search(question))
    wants_world_bank = bool(GDP_PER_CAPITA.search(question))
    if (
        not UNITED_STATES.search(geography)
        or wants_fred == wants_world_bank
        or UNSUPPORTED.search(question)
    ):
        return abstain(
            "No single supported product covers this question. Ask about a U.S. unemployment rate or U.S. GDP per capita.",
            "product selection",
            start,
        )

    item = product(FRED_ID if wants_fred else WORLD_BANK_ID)
    years = YEAR.findall(question)
    months = months_in(question)
    if len(years) != 1 or (wants_fred and len(months) != 1) or (wants_world_bank and months):
        detail = (
            "Ask for one month and year, such as April 2020."
            if wants_fred
            else "Ask for one calendar year; this GDP-per-capita series is annual."
        )
        return abstain(detail, "field selection", start)

    year = int(years[0])
    if wants_fred:
        observed = date(year, months[0], 1)
        first = date.fromisoformat(item["snapshot"]["start"])
        last = date.fromisoformat(item["snapshot"]["end"])
        in_range = first <= observed <= last
        coverage = f"{first:%B %Y} through {last:%B %Y}"
        query_key = observed.isoformat()
        period = f"{observed:%B %Y}"
    else:
        first_year = int(item["snapshot"]["start"])
        last_year = int(item["snapshot"]["end"])
        in_range = first_year <= year <= last_year
        coverage = f"{first_year} through {last_year}"
        query_key = str(year)
        period = str(year)

    if not in_range:
        return abstain(f"The local snapshot covers {coverage}.", "coverage check", start)

    selected_fields = [column["name"] for column in item["tables"][0]["columns"]]
    return {
        "outcome": "selected",
        "product": {
            "id": item["id"],
            "version": item["version"],
            "name": item["name"],
            "description": item["description"],
            "coverage": item["business_context"],
            "coverage_range": coverage,
            "source_url": item["source"]["url"],
            "snapshot_date": item["snapshot"]["retrieved_at"],
        },
        "selected_fields": selected_fields,
        "query_key": query_key,
        "reason": f"This product contains the requested measure for {period}.",
        "trace": [
            {"stage": "product selection", "result": item["name"]},
            {"stage": "coverage check", "result": f"{period} is in the local snapshot"},
            {"stage": "field selection", "result": ", ".join(selected_fields)},
        ],
        "latency_ms": round((time.perf_counter() - start) * 1000, 1),
    }


def preview(question: str, use_retrieval: bool = False) -> dict[str, Any]:
    if not use_retrieval:
        return _preview_local(question)
    from .retrieval import search

    start = time.perf_counter()
    candidates = search(question)
    proposal = _preview_local(question)
    proposal["retrieved_products"] = candidates
    proposal["trace"].insert(0, {
        "stage": "SingleStore vector retrieval",
        "result": "; ".join(f"{item['name']} ({item['score']:.3f})" for item in candidates) or "No indexed products",
    })
    if proposal["outcome"] == "selected" and not any(
        item["id"] == proposal["product"]["id"] and item["version"] == proposal["product"]["version"]
        for item in candidates
    ):
        proposal = {
            "outcome": "abstain",
            "reason": "No retrieved product supports this question. Re-ingest the catalog if metadata changed.",
            "retrieved_products": candidates,
            "trace": proposal["trace"][:1],
        }
    if proposal["outcome"] == "abstain":
        proposal["candidates"] = candidates
    proposal["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return proposal


def execute(question: str, product_id: str, manifest_version: int) -> dict[str, Any]:
    proposal = preview(question)
    if proposal["outcome"] != "selected":
        raise ValueError(proposal["reason"])
    if proposal["product"]["id"] != product_id or proposal["product"]["version"] != manifest_version:
        raise ValueError("The selected product changed. Review the proposal again.")

    item = product(product_id)
    parquet = local_path(item["tables"][0]["path"])
    key = proposal["query_key"]
    if product_id == FRED_ID:
        table = "unemployment_rate"
        sql = (
            "SELECT observation_date, unemployment_rate\n"
            "FROM unemployment_rate\n"
            f"WHERE observation_date = DATE '{key}'\n"
            "LIMIT 1"
        )
    elif product_id == WORLD_BANK_ID:
        table = "gdp_per_capita"
        sql = (
            "SELECT year, gdp_per_capita_usd\n"
            "FROM gdp_per_capita\n"
            f"WHERE year = {key}\n"
            "LIMIT 1"
        )
    else:
        raise ValueError("This product does not have an execution adapter yet.")

    started = time.perf_counter()
    connection = duckdb.connect()
    try:
        # Table names are fixed adapters; paths are validated inside the repository.
        connection.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?)", [str(parquet)])
        connection.execute("SET enable_external_access = false")
        row = connection.execute(sql).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("No observation was found for that period.")

    value = float(row[1])
    if product_id == FRED_ID:
        answer = f"The U.S. unemployment rate in {date.fromisoformat(key):%B %Y} was {value:g}%."
        result_row = {"observation_date": str(row[0]), "unemployment_rate": value}
    else:
        answer = f"U.S. GDP per capita in {key} was ${value:,.2f} (current U.S. dollars)."
        result_row = {"year": int(row[0]), "gdp_per_capita_usd": value}

    return {
        "outcome": "answered",
        "answer": answer,
        "sql": sql,
        "rows": [result_row],
        "product": proposal["product"],
        "selected_fields": proposal["selected_fields"],
        "trace": proposal["trace"]
        + [
            {"stage": "human confirmation", "result": "Confirmed product and manifest version"},
            {"stage": "SQL generation", "result": "Read-only query over selected fields"},
            {"stage": "DuckDB execution", "result": "1 observation returned"},
        ],
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }
