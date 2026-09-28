"""Browser-facing FastAPI service, reached through the Next.js proxy."""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import singlestoredb as s2

from .catalog import CatalogError, catalog
from .embeddings import EmbeddingError
from .retrieval import search
from .inspection import public_product
from .reviews import router as review_router
from .sql_runs import router as sql_router
from .workflows import router as workflow_router
from .conversations_api import router as conversation_router
from .singlestore import ConfigurationError


app = FastAPI(title="DataScout API", version="0.1.0")
app.include_router(review_router)
app.include_router(sql_router)
app.include_router(workflow_router)
app.include_router(conversation_router)
logger = logging.getLogger("uvicorn.error")


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class SearchQuestion(Question):
    top_k: int = Field(default=3, ge=1, le=10)


@app.post("/retrieval/search")
def search_products(request: SearchQuestion) -> dict:
    try:
        products = search(request.question, request.top_k)
        logger.info("retrieval question=%r products=%s", request.question, products)
        return {"products": products}
    except (ConfigurationError, EmbeddingError, s2.Error) as error:
        raise HTTPException(status_code=503, detail="Retrieval unavailable; check API configuration and catalog ingestion.") from error
    except CatalogError as error:
        raise HTTPException(status_code=503, detail="Catalog unavailable; check the API logs.") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/catalog")
def get_catalog() -> list[dict]:
    try:
        return [public_product(item) for item in catalog()]
    except CatalogError as error:
        raise HTTPException(status_code=503, detail="Catalog unavailable; check the API logs.") from error
