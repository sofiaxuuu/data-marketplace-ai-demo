"""Browser-facing FastAPI service, reached through the Next.js proxy."""

from __future__ import annotations

import os
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import singlestoredb as s2

from .analysis import execute, preview
from .catalog import ROOT, CatalogError, catalog
from .embeddings import EmbeddingError
from .retrieval import search
from .inspection import public_product
from .reviews import router as review_router
from .singlestore import ConfigurationError


app = FastAPI(title="DataScout API", version="0.1.0")
app.include_router(review_router)
logger = logging.getLogger("uvicorn.error")


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class Confirmation(Question):
    product_id: str
    manifest_version: int


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


@app.post("/runs/preview")
def preview_run(request: Question) -> dict:
    try:
        load_dotenv(ROOT / ".env", override=False)
        result = preview(request.question, use_retrieval=os.environ.get("DATASCOUT_RETRIEVAL_BACKEND") == "singlestore")
        if "retrieved_products" in result:
            logger.info("retrieval question=%r products=%s", request.question, result["retrieved_products"])
        return result
    except (ConfigurationError, EmbeddingError, s2.Error) as error:
        raise HTTPException(status_code=503, detail="Retrieval unavailable; check API configuration and catalog ingestion.") from error
    except CatalogError as error:
        raise HTTPException(status_code=503, detail="Catalog unavailable; check the API logs.") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/runs/execute")
def execute_run(request: Confirmation) -> dict:
    try:
        return execute(request.question, request.product_id, request.manifest_version)
    except CatalogError as error:
        raise HTTPException(status_code=503, detail="Catalog unavailable; check the API logs.") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
