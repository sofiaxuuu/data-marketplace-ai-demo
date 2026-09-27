"""Declarative ingestion recipes. Configurations contain data, never code."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[3]
IDENTIFIER = r"^[a-z][a-z0-9_]{0,99}$"
JsonPath = list[str | int]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Column(StrictModel):
    name: str = Field(pattern=IDENTIFIER)
    type: Literal["DATE", "INTEGER", "BIGINT", "DOUBLE", "VARCHAR"]
    description: str
    unit: str | None = None
    paths: list[JsonPath] = Field(default_factory=list)
    nullable: bool = False
    scale: float = Field(default=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def scale_is_numeric(self):
        if self.type in ("DATE", "VARCHAR") and self.scale != 1:
            raise ValueError("Scaling applies only to numeric columns")
        return self


class Check(StrictModel):
    path: JsonPath
    equals: str | int | float


class RowFilter(StrictModel):
    path: JsonPath
    minimum: str | int | float
    maximum: str | int | float


class Pagination(StrictModel):
    mode: Literal["none", "page"] = "none"
    page_parameter: str = "page"
    first_page: int = Field(default=1, ge=0)
    total_pages_path: JsonPath = Field(default_factory=list)
    max_pages: int = Field(default=100, ge=1, le=1000)


class FiscalPeriod(StrictModel):
    fiscal_year: int = Field(ge=1900, le=2100)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    start: date | None = None
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start and (self.start > self.end or not 60 <= (self.end - self.start).days <= 380):
            raise ValueError("Invalid fiscal duration")
        if self.fiscal_quarter and (not self.start or not 60 <= (self.end - self.start).days <= 110):
            raise ValueError("Quarterly periods require exact three-month duration")
        return self


class Source(StrictModel):
    adapter: Literal["csv", "csv_zip", "rest_json", "sec_xbrl"]
    url: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    params: dict[str, str | int] = Field(default_factory=dict)
    headers_env: dict[str, str] = Field(default_factory=dict)
    records_path: JsonPath = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    record_checks: list[Check] = Field(default_factory=list)
    pagination: Pagination = Field(default_factory=Pagination)
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8-sig"
    # Specialized XBRL reader options, not a dataset/company allowlist.
    cik: int | None = Field(default=None, gt=0)
    accession: str | None = None
    filing_date: date | None = None
    currency: Literal["USD"] = "USD"
    period_ends: dict[int, date] = Field(default_factory=dict)
    form: Literal["10-K", "10-Q"] = "10-K"
    periods: list[FiscalPeriod] = Field(default_factory=list)
    metrics: dict[str, str] = Field(default_factory=dict)
    period_basis: str | None = None

    @model_validator(mode="after")
    def supported_options(self):
        if self.adapter == "sec_xbrl":
            if self.url or self.params or self.headers_env or self.pagination.mode != "none":
                raise ValueError("EdgarTools manages SEC transport; do not configure HTTP options")
            if not self.cik or not self.accession or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", self.accession) or not self.filing_date:
                raise ValueError("SEC requires CIK, pinned accession and filing date")
            if not (self.period_ends or self.periods) or not self.metrics or not self.period_basis:
                raise ValueError("SEC requires reviewed fiscal periods, concepts and period basis")
            if self.period_ends and self.periods:
                raise ValueError("Use either legacy annual ends or explicit periods")
            if self.periods:
                keys = [(p.fiscal_year, p.fiscal_quarter) for p in self.periods]
                shapes = {(p.start is not None, p.fiscal_quarter is not None) for p in self.periods}
                if len(keys) != len(set(keys)) or len(shapes) != 1:
                    raise ValueError("Fiscal periods must be unique and use one consistent shape")
            if len(set(self.period_ends.values())) != len(self.period_ends):
                raise ValueError("Fiscal period end dates must be unique")
            for key, concept in self.metrics.items():
                if not re.fullmatch(IDENTIFIER, key) or not re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_]+", concept):
                    raise ValueError("Invalid XBRL concept mapping")
        else:
            parsed = urlsplit(self.url or "")
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
                raise ValueError("Use a public HTTPS URL without credentials/query; put query values in params")
            if parsed.hostname not in self.allowed_hosts:
                raise ValueError("Endpoint host must be explicitly approved in allowed_hosts")
            if self.cik or self.accession or self.period_ends or self.periods or self.metrics or self.form != "10-K":
                raise ValueError("XBRL options require sec_xbrl")
            if any(re.search(r"token|secret|password|api.?key|authorization", key, re.I) for key in self.params):
                raise ValueError("Use headers_env for credentials, not literal query parameters")
            if self.adapter in ("csv", "csv_zip") and (self.pagination.mode != "none" or self.records_path or self.checks):
                raise ValueError("CSV does not support JSON response options or pagination")
            if self.pagination.mode == "page" and not self.pagination.total_pages_path:
                raise ValueError("Page pagination requires total_pages_path")
        return self


class Coverage(StrictModel):
    column: str = Field(pattern=IDENTIFIER)
    start: str
    end: str
    frequency: Literal["monthly", "annual", "bounds"]


class Recipe(StrictModel):
    recipe_version: Literal[1]
    id: str = Field(pattern=IDENTIFIER)
    version: int = Field(default=1, ge=1)
    name: str
    description: str
    business_context: str
    concepts: list[str]
    facets: dict[str, str] = Field(default_factory=dict)
    table_id: str = Field(pattern=IDENTIFIER)
    columns: list[Column] = Field(min_length=1)
    source_name: str
    source_url: str
    source: Source
    unique_key: list[str] = Field(min_length=1)
    coverage: Coverage
    filters: list[RowFilter] = Field(default_factory=list)
    skip_missing: list[JsonPath] = Field(default_factory=list)
    missing_values: list[str] = Field(default_factory=lambda: ["", "."])
    expected_rows: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_columns(self):
        names = [col.name for col in self.columns]
        if len(names) != len(set(names)) or not set(self.unique_key + [self.coverage.column]) <= set(names):
            raise ValueError("Columns must be unique; key and coverage columns must exist")
        citation = urlsplit(self.source_url)
        if citation.scheme != "https" or not citation.hostname or citation.username or citation.password:
            raise ValueError("Source attribution must be a public HTTPS URL without credentials")
        if self.source.adapter == "sec_xbrl":
            periods = self.source.periods
            expected = [("fiscal_year", "INTEGER")]
            if periods and periods[0].fiscal_quarter:
                expected.append(("fiscal_quarter", "INTEGER"))
            if not periods or periods[0].start:
                expected.append(("period_start", "DATE"))
            expected += [("period_end", "DATE"), *[(key, "BIGINT") for key in self.source.metrics]]
            if [(col.name, col.type) for col in self.columns] != expected:
                raise ValueError("SEC columns must match configured metric order and types")
        elif any(not col.paths for col in self.columns):
            raise ValueError("CSV/JSON columns require source paths")
        if self.coverage.frequency == "monthly":
            first, last = date.fromisoformat(self.coverage.start), date.fromisoformat(self.coverage.end)
            if first.day != 1 or last.day != 1 or first > last or (last - first).days > 365 * 200:
                raise ValueError("Use valid first-of-month coverage within 200 years")
        elif self.coverage.frequency == "annual":
            if not 0 <= int(self.coverage.start) <= int(self.coverage.end) <= 9999:
                raise ValueError("Use ordered annual coverage within years 0–9999")
        if any(len(rule.path) != 1 or rule.path[0] not in names for rule in self.filters):
            raise ValueError("Filters reference normalized output column names")
        return self


def load_recipe(dataset: str, root: Path = ROOT) -> Recipe:
    if not re.fullmatch(IDENTIFIER, dataset):
        raise ValueError("Use a dataset ID, not a path or URL")
    path = root / "data/recipes" / f"{dataset}.yaml"
    if not path.is_file():
        raise ValueError("No reviewed recipe exists for this dataset")
    recipe = Recipe.model_validate(yaml.safe_load(path.read_text()))
    if recipe.id != dataset:
        raise ValueError("Recipe ID must match filename")
    return recipe
