"""Reusable CSV/JSON readers and a filing-specific XBRL normalizer."""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import os
import socket
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import certifi
import httpx
from dotenv import load_dotenv

from .models import ROOT, Recipe, Source

MAX_BYTES = 20 * 1024 * 1024
ENVELOPE = "datascout-source-v1"


def at(value, path: list):
    for key in path:
        try:
            if isinstance(key, int) and isinstance(value, list):
                value = value[key]
            elif isinstance(key, str) and isinstance(value, dict):
                value = value[key]
            else:
                raise ValueError("Response path does not match its structure")
        except (KeyError, IndexError):
            raise ValueError("Configured response field is missing") from None
    return value


def check_public_url(url: str, allowed_hosts: list[str]) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
        raise ValueError("URL is outside reviewed HTTPS hosts")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("Source host cannot be resolved") from None
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private, loopback and reserved source addresses are not allowed")


def http_get(client: httpx.Client, source: Source, params: dict, headers: dict) -> bytes:
    url = source.url
    for _ in range(4):
        check_public_url(url, source.allowed_hosts)
        with client.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                # Do not forward credentials to a different host, even an approved one.
                target = urljoin(str(response.url), response.headers.get("location", ""))
                if urlsplit(target).hostname != urlsplit(url).hostname:
                    raise ValueError("Cross-host redirects require a separate reviewed endpoint")
                url, params = target, {}
                continue
            if response.status_code >= 400:
                raise ValueError(f"Source returned HTTP {response.status_code}")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Source response exceeds the 20 MiB snapshot limit")
                chunks.append(chunk)
            return b"".join(chunks)
    raise ValueError("Source has too many redirects")


def acquire(recipe: Recipe, client: httpx.Client | None = None) -> bytes:
    load_dotenv(ROOT / ".env")
    source = recipe.source
    if source.adapter == "sec_xbrl":
        return acquire_sec(recipe)
    headers = {}
    for header, env_name in source.headers_env.items():
        value = os.getenv(env_name)
        if not value:
            raise ValueError("A required source credential environment variable is not configured")
        headers[header] = value
    own_client = client is None
    client = client or httpx.Client(verify=certifi.where(), timeout=30, trust_env=False)
    try:
        params = dict(source.params)
        if source.pagination.mode == "page":
            params[source.pagination.page_parameter] = source.pagination.first_page
        raw = http_get(client, source, params, headers)
        if source.adapter == "csv":
            return raw
        first = json.loads(raw, parse_float=Decimal)
        pages = [raw.decode("utf-8-sig")]
        total_bytes = len(raw)
        pagination = source.pagination
        if pagination.mode == "page":
            count = Decimal(str(at(first, pagination.total_pages_path)))
            if not count.is_finite() or count != count.to_integral_value() or not 1 <= count <= pagination.max_pages:
                raise ValueError("Invalid or excessive pagination count")
            for page in range(pagination.first_page + 1, pagination.first_page + int(count)):
                params = {**source.params, pagination.page_parameter: page}
                page_raw = http_get(client, source, params, headers)
                total_bytes += len(page_raw)
                if total_bytes > MAX_BYTES:
                    raise ValueError("Combined pages exceed the 20 MiB snapshot limit")
                payload = json.loads(page_raw, parse_float=Decimal)
                if Decimal(str(at(payload, pagination.total_pages_path))) != count:
                    raise ValueError("Pagination metadata changed during download")
                pages.append(page_raw.decode("utf-8-sig"))
        return json.dumps({"format": ENVELOPE, "adapter": "rest_json", "pages": pages}, separators=(",", ":"), allow_nan=False).encode()
    finally:
        if own_client:
            client.close()


def acquire_sec(recipe: Recipe) -> bytes:
    source = recipe.source
    identity = os.getenv("EDGAR_IDENTITY", "").strip()
    if not identity or "@" not in identity:
        raise ValueError("Set EDGAR_IDENTITY to your name and contact email in .env")
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(ROOT / ".cache/edgar")
    from edgar import Company, set_identity

    set_identity(identity)
    filing = Company(source.cik).get_filings(form=source.form, filing_date=source.filing_date.isoformat()).get(source.accession)
    if filing is None or filing.accession_no != source.accession or filing.form != source.form:
        raise ValueError("Pinned filing was not found; latest-filing fallback is prohibited")
    xbrl = filing.xbrl()
    if xbrl is None:
        raise ValueError("Pinned filing has no parseable XBRL")
    payload = {
        "cik": source.cik, "accession": source.accession,
        "filing_date": source.filing_date.isoformat(), "source_url": recipe.source_url,
        "retrieved_at": date.today().isoformat(), "edgartools_version": version("edgartools"),
        "artifact_kind": "extracted-concept-facts",
        "facts": [fact for fact in xbrl.facts.get_facts()
                  if str(fact.get("concept", "")).replace("_", ":", 1) in source.metrics.values()],
    }
    return (json.dumps(payload, indent=2, default=str, allow_nan=False) + "\n").encode()


def records(recipe: Recipe, raw: bytes) -> list[dict]:
    if len(raw) > MAX_BYTES:
        raise ValueError("Source exceeds the 20 MiB snapshot limit")
    source = recipe.source
    if source.adapter == "csv":
        reader = csv.DictReader(io.StringIO(raw.decode(source.encoding)), delimiter=source.delimiter)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)):
            raise ValueError("CSV contains duplicate headers")
        for column in recipe.columns:
            if not any(len(path) == 1 and path[0] in fields for path in column.paths):
                raise ValueError("CSV does not contain the configured columns")
        result = list(reader)
        if any(None in row for row in result):
            raise ValueError("CSV row has more fields than its header")
        return result
    payload = json.loads(raw, parse_float=Decimal)
    if source.adapter == "sec_xbrl":
        if payload.get("cik") != source.cik or payload.get("accession") != source.accession:
            raise ValueError("Input does not belong to the configured SEC filing")
        return sec_records(source, payload["facts"])
    if isinstance(payload, dict) and payload.get("format") == ENVELOPE:
        if payload.get("adapter") != "rest_json":
            raise ValueError("Saved envelope belongs to a different adapter")
        pages = [json.loads(page, parse_float=Decimal) if isinstance(page, str) else page for page in payload["pages"]]
    else:
        pages = [payload]
    if not pages or len(pages) > source.pagination.max_pages:
        raise ValueError("Invalid saved pagination envelope")
    if source.pagination.mode == "page":
        for page in pages:
            if Decimal(str(at(page, source.pagination.total_pages_path))) != len(pages):
                raise ValueError("Saved response is missing pages")
    result = []
    for page in pages:
        for check in source.checks:
            if at(page, check.path) != check.equals:
                raise ValueError("Source response failed a configured assertion")
        items = at(page, source.records_path)
        if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
            raise ValueError("Configured records path must contain objects")
        result.extend(items)
    return result


def sec_records(source: Source, facts: list[dict]) -> list[dict]:
    result = []
    periods = [(p.fiscal_year, p.end, p.start, p.fiscal_quarter, p.start is None)
               for p in source.periods] or [(year, end, None, None, False) for year, end in sorted(source.period_ends.items())]
    for year, end, exact_start, quarter, instant in periods:
        row = {"fiscal_year": year, "period_end": end}
        if quarter:
            row["fiscal_quarter"] = quarter
        starts = set()
        for column, concept in source.metrics.items():
            matches = set()
            for fact in facts:
                if str(fact.get("concept", "")).replace("_", ":", 1) != concept:
                    continue
                if fact.get("period_type") != ("instant" if instant else "duration") or str(fact.get("period_instant" if instant else "period_end")) != end.isoformat():
                    continue
                if any(key.startswith("dim_") and value for key, value in fact.items()) or fact.get("dimensions"):
                    continue
                if fact.get("currency") != source.currency or str(fact.get("entity_identifier", "")).lstrip("0") != str(source.cik):
                    continue
                start = None if instant else date.fromisoformat(str(fact["period_start"]))
                if not instant and (start != exact_start if exact_start else not 350 <= (end - start).days <= 380):
                    continue
                try:
                    number = Decimal(str(fact["numeric_value"]))
                except (InvalidOperation, KeyError):
                    raise ValueError("Invalid XBRL numeric fact") from None
                if not number.is_finite() or number != number.to_integral_value():
                    raise ValueError("Expected finite whole-dollar XBRL values")
                matches.add((start, int(number)))
            if len(matches) != 1:
                raise ValueError(f"Expected one unambiguous {column} total for FY{year}; found {len(matches)}")
            start, row[column] = matches.pop()
            starts.add(start)
        if len(starts) != 1:
            raise ValueError("Metrics have mismatched fiscal reporting periods")
        start = starts.pop()
        if not instant:
            row["period_start"] = start
        result.append(row)
    return result
