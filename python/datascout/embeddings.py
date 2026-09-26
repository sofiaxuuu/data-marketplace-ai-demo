"""Hosted embedding adapter for the V1 retrieval baseline."""

from __future__ import annotations

import math
import os

import httpx
from dotenv import load_dotenv

from .catalog import ROOT
from .singlestore import ConfigurationError

MODEL = "text-embedding-3-small"
DIMENSIONS = 1536


class EmbeddingError(RuntimeError):
    pass


def normalized(vector: list[float]) -> list[float]:
    if len(vector) != DIMENSIONS or not all(math.isfinite(value) for value in vector):
        raise EmbeddingError("Embedding has invalid dimensions or non-finite values.")
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        raise EmbeddingError("Embedding cannot be a zero vector.")
    return [value / magnitude for value in vector]


def embed(texts: list[str]) -> list[list[float]]:
    load_dotenv(ROOT / ".env", override=False)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ConfigurationError("Set OPENAI_API_KEY in the local .env file.")
    if not texts or any(not text.strip() for text in texts):
        raise EmbeddingError("Embedding inputs must contain text.")
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": MODEL, "input": texts, "dimensions": DIMENSIONS, "encoding_format": "float"},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as error:
        raise EmbeddingError(f"Embedding API returned HTTP {error.response.status_code}.") from None
    except (httpx.RequestError, ValueError):
        raise EmbeddingError("Embedding API request failed; check network access and configuration.") from None
    data = sorted(payload.get("data", []), key=lambda item: item["index"])
    if len(data) != len(texts) or [item["index"] for item in data] != list(range(len(texts))):
        raise EmbeddingError("Embedding API returned an incomplete batch.")
    return [normalized(item["embedding"]) for item in data]
