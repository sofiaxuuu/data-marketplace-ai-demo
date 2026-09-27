"""Candidate-only discovery adapter: never fetch arbitrary URLs or acquire data."""
from __future__ import annotations

import ipaddress
import os
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from .catalog import ROOT


class DiscoveryError(RuntimeError):
    pass


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    publisher: str
    url: str
    evidence: str
    relevance: str
    provider: str
    discovered_at: str
    coverage: str = "Unknown — requires source review"
    units: str = "Unknown — requires source review"
    access: str = "Unknown — requires source review"
    licensing: str = "Unknown — requires source review"


class SearchProvider(Protocol):
    name: str

    def search(self, question: str) -> list[Candidate]: ...


def public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".")
        parsed.port  # reject malformed port syntax
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or len(value) > 2048:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and not all(part.isdigit() for part in host.split("."))
    except ValueError:
        return False


class ExaProvider:
    name = "exa"

    def search(self, question: str) -> list[Candidate]:
        load_dotenv(ROOT / ".env", override=False)
        key = os.environ.get("EXA_API_KEY")
        if not key:
            raise DiscoveryError("Set EXA_API_KEY in the server environment for external discovery.")
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.post("https://api.exa.ai/search", headers={"x-api-key": key}, json={
                    "query": question + "\nFind authoritative primary-publisher datasets, official APIs or dataset documentation; prefer original sources over articles.",
                    "type": "auto", "numResults": 5,
                    "contents": {"highlights": {"maxCharacters": 1200}},
                })
                response.raise_for_status()
                rows = response.json()["results"]
            if not isinstance(rows, list):
                raise ValueError("Invalid results")
            now, seen, candidates = datetime.now(timezone.utc).isoformat(), set(), []
            for row in rows[:5]:
                if not isinstance(row, dict):
                    raise ValueError("Invalid candidate")
                url, title, highlights = row.get("url", ""), row.get("title", ""), row.get("highlights", [])
                if not isinstance(url, str) or not isinstance(title, str) or not isinstance(highlights, list) or not all(isinstance(x, str) for x in highlights):
                    raise ValueError("Invalid evidence")
                if not public_url(url) or url in seen:
                    continue
                seen.add(url)
                candidates.append(Candidate(title=title[:300] or "Untitled source", publisher=urlsplit(url).hostname or "Unknown",
                    url=url, evidence="\n".join(highlights)[:1200] or "No excerpt returned; inspect the original source.",
                    relevance="Search match only; suitability and publisher authority require human review.",
                    provider=self.name, discovered_at=now))
            return candidates
        except httpx.HTTPStatusError as exc:
            raise DiscoveryError(f"External search returned HTTP {exc.response.status_code}. Check Exa configuration.") from None
        except (httpx.RequestError, ValueError, KeyError, TypeError, AttributeError):
            raise DiscoveryError("External search failed or returned invalid results. No source was acquired.") from None
