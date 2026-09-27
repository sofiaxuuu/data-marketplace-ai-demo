"""Bounded, explicitly approved public CSV acquisition for one-off analysis."""
from __future__ import annotations

import hashlib
import html.parser
import http.client
import io
import ipaddress
import os
import re
import shutil
import socket
import ssl
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import duckdb

from .catalog import ROOT
from .discovery import public_url

BASE = ROOT / ".local" / "external-runs"
PAGE_LIMIT = 2_000_000
DOWNLOAD_LIMIT = 25_000_000
EXTRACT_LIMIT = 250_000_000
MAX_FILES = 30


class ExternalFileError(RuntimeError):
    pass


class Links(html.parser.HTMLParser):
    def __init__(self, base: str):
        super().__init__()
        self.base, self.links = base, []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if not isinstance(href, str):
            return
        target = urljoin(self.base, href)
        if supported_file(target):
            self.links.append(target)


def supported_file(url: str) -> bool:
    parsed = urlsplit(url)
    return public_url(url) and parsed.port in (None, 443) and not parsed.query and not parsed.fragment and unquote(parsed.path).lower().endswith((".csv", ".zip"))


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, port=443, timeout=20, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def fetch(url: str, limit: int) -> bytes:
    if not public_url(url):
        raise ExternalFileError("Only public HTTPS sources are supported.")
    parsed = urlsplit(url)
    if parsed.port not in (None, 443) or not parsed.hostname or parsed.query or parsed.fragment:
        raise ExternalFileError("Only public standard HTTPS URLs without query credentials are supported.")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ExternalFileError("Source host did not resolve exclusively to public addresses.")
        conn = PinnedHTTPS(parsed.hostname, sorted(addresses)[0])
        try:
            path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            conn.request("GET", path, headers={"User-Agent": "DataScout/0.1 dataset review", "Accept": "text/html,text/csv,application/zip,*/*;q=0.1"})
            response = conn.getresponse()
            if response.status != 200:
                raise ExternalFileError(f"Source returned HTTP {response.status}; redirects are not followed.")
            if int(response.getheader("Content-Length") or 0) > limit:
                raise ExternalFileError("Source exceeds the size limit.")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ExternalFileError("Source exceeds the size limit.")
            return data
        finally:
            conn.close()
    except ExternalFileError:
        raise
    except (OSError, ssl.SSLError, ValueError, http.client.HTTPException):
        raise ExternalFileError("Could not safely read the selected public source.") from None


def inspect_page(url: str, question: str) -> list[dict]:
    if supported_file(url):
        return [{"url": url, "name": Path(urlsplit(url).path).name}]
    raw = fetch(url, PAGE_LIMIT)
    parser = Links(url)
    parser.feed(raw.decode("utf-8", errors="replace"))
    unique = list(dict.fromkeys(parser.links))
    years = re.findall(r"\b(?:19|20)\d{2}\b", question)
    terms = [t.lower() for t in re.findall(r"[a-zA-Z]{4,}", question) if t.lower() not in {"where", "find", "data", "measurements"}]
    def score(link):
        path = unquote(urlsplit(link).path).lower()
        return sum(5 for year in years if year in path) + sum(2 for term in terms if term in path) + (2 if "daily" in question.lower() and "daily" in path else 0)
    unique.sort(key=lambda link: -score(link))
    return [{"url": link, "name": Path(unquote(urlsplit(link).path)).name} for link in unique[:MAX_FILES]]


def acquire(run_id: str, url: str) -> dict:
    if not supported_file(url):
        raise ExternalFileError("Approve a direct public CSV or ZIP file.")
    raw = fetch(url, DOWNLOAD_LIMIT)
    if raw.startswith(b"PK\x03\x04"):
        csv_bytes = extract_csv(raw)
    elif urlsplit(url).path.lower().endswith(".csv"):
        csv_bytes = raw
    else:
        raise ExternalFileError("Selected file is not a CSV or ZIP archive.")
    if b"\x00" in csv_bytes[:10000] or not csv_bytes.strip():
        raise ExternalFileError("Selected file is not a usable text CSV.")
    if csv_bytes.lstrip()[:30].lower().startswith((b"<html", b"<!doctype html")):
        raise ExternalFileError("Selected link returned an HTML page, not CSV data.")
    target = BASE / run_id
    target.mkdir(parents=True, exist_ok=True)
    # Atomic replacement; no untrusted filename is used locally.
    fd, filename = tempfile.mkstemp(prefix="incoming-", suffix=".csv", dir=target)
    os.close(fd)
    temporary = Path(filename)
    try:
        temporary.write_bytes(csv_bytes)
        conn = duckdb.connect()
        try:
            columns = conn.execute("DESCRIBE SELECT * FROM read_csv(?)", [str(temporary)]).fetchall()
            if not columns or len(columns) > 100:
                raise ExternalFileError("CSV has no usable schema or too many columns.")
            count = conn.execute("SELECT COUNT(*) FROM read_csv(?)", [str(temporary)]).fetchone()[0]
            if not 0 < count <= 1_000_000:
                raise ExternalFileError("CSV is empty or exceeds one million rows.")
            names = [re.sub(r"[^a-z0-9]+", "_", row[0].lower()).strip("_") for row in columns]
            if len(set(names)) != len(names) or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", name) for name in names):
                raise ExternalFileError("CSV columns need a reviewed schema mapping.")
            projection = ", ".join('"' + row[0].replace('"', '""') + '" AS "' + name + '"' for row, name in zip(columns, names))
            parquet = target / "snapshot.parquet"
            conn.execute(f"COPY (SELECT {projection} FROM read_csv(?)) TO '{str(parquet).replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)", [str(temporary)])
            date_name = next((name for name, row in zip(names, columns) if row[1].upper() == "DATE"), None)
            if date_name:
                start, end = conn.execute(f'SELECT MIN("{date_name}"), MAX("{date_name}") FROM read_parquet(?)', [str(parquet)]).fetchone()
            else:
                start, end = None, None
            if not start or not end:
                raise ExternalFileError("Could not verify date coverage from this CSV; choose a source with an explicit date column.")
            unit_name = next((name for name in names if name in ("units_of_measure", "unit", "units")), None)
            units = [str(row[0])[:160] for row in conn.execute(f'SELECT DISTINCT "{unit_name}" FROM read_parquet(?) LIMIT 6', [str(parquet)]).fetchall()] if unit_name else []
            sample = conn.execute("SELECT * FROM read_parquet(?) LIMIT 3", [str(parquet)]).fetchall()
        finally:
            conn.close()
        digest = hashlib.sha256(parquet.read_bytes()).hexdigest()
        (target / "source.csv").write_bytes(csv_bytes)
        types = {"INTEGER", "BIGINT", "DOUBLE", "FLOAT", "DATE", "VARCHAR", "DECIMAL"}
        if any(row[1].upper().split("(")[0] not in types for row in columns):
            raise ExternalFileError("CSV contains unsupported column types.")
        return {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": digest, "rows": count,
                "raw_sha256": hashlib.sha256(csv_bytes).hexdigest(),
                "start": start.isoformat() if start else "Unknown", "end": end.isoformat() if end else "Unknown",
                "units_observed": units, "sample_rows": [{name: str(value)[:160] if value is not None else None for name, value in zip(names, row)} for row in sample],
                "columns": [{"name": name, "type": kind.upper(), "description": original} for name, (original, kind, *_) in zip(names, columns)]}
    except duckdb.Error:
        raise ExternalFileError("CSV could not be parsed or normalized safely.") from None
    finally:
        temporary.unlink(missing_ok=True)


def remove(run_id: str):
    if re.fullmatch(r"[0-9a-f-]{36}", run_id):
        shutil.rmtree(BASE / run_id, ignore_errors=True)


def extract_csv(raw: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].is_dir() or not entries[0].filename.lower().endswith(".csv"):
                raise ExternalFileError("ZIP must contain exactly one CSV file.")
            entry = entries[0]
            if Path(entry.filename).name != entry.filename or "\\" in entry.filename or entry.file_size > EXTRACT_LIMIT or entry.compress_size > DOWNLOAD_LIMIT:
                raise ExternalFileError("ZIP contains an unsafe or oversized entry.")
            with archive.open(entry) as stream:
                data = stream.read(EXTRACT_LIMIT + 1)
            if len(data) > EXTRACT_LIMIT:
                raise ExternalFileError("Extracted CSV exceeds the size limit.")
            return data
    except ExternalFileError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise ExternalFileError("Invalid ZIP file.") from None
