"""Local LangGraph orchestration with durable human gates and bounded specialists.

Graph checkpoints own analysis state; the run/action ledger owns concurrency and
idempotency. SQL remains bound to the existing saved-plan store. No implicit retry.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Annotated, Literal, TypedDict

from fastapi import APIRouter, BackgroundTasks, HTTPException
from filelock import FileLock, Timeout
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import sql_runs
from .catalog import ROOT, CatalogError, catalog, product
from .discovery import DiscoveryError, ExaProvider
from .external_advice import ExternalAdviceError, recommend as recommend_external
from .external_files import ExternalFileError, inspect_page, acquire, remove as remove_external, BASE as EXTERNAL_BASE
from .external_registration import proposal as registration_proposal, register as register_external
from .inspection import public_product
from .source_advice import AdviceError, advise, validate_advice

router = APIRouter(prefix="/workflows")
DATABASE = ROOT / ".local" / "workflows.sqlite3"
RUN_TTL = 86400


class State(TypedDict, total=False):
    run_id: str
    question: str
    catalog_fingerprint: str
    selected: dict | None
    selected_fingerprint: str | None
    advice: dict | None
    plan: dict | None
    result: dict | None
    external: list[dict] | None
    external_advice: dict | None
    external_links: list[dict] | None
    external_index: int | None
    approved_external_url: str | None
    external_file: dict | None
    registration: dict | None
    rejected: list[str]
    recovery_reason: str
    confirmed: bool
    approved: bool
    stage: str
    error: str | None
    retry_node: str | None
    command: dict
    trace: list[dict]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(Strict):
    request_id: uuid.UUID
    question: str = Field(min_length=1, max_length=1000)
    product_id: str | None = Field(default=None, max_length=128)
    manifest_version: int | None = Field(default=None, ge=1)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Enter a question")
        return value.strip()

    @model_validator(mode="after")
    def paired_source(self):
        if (self.product_id is None) != (self.manifest_version is None):
            raise ValueError("Product and version must be supplied together")
        return self


class Select(Strict):
    type: Literal["select_source"]
    product_id: str = Field(min_length=1, max_length=128)
    manifest_version: int = Field(ge=1)


class Confirm(Strict):
    type: Literal["confirm_source"]
    confirmed: Literal[True]


class Approve(Strict):
    type: Literal["approve_sql"]
    approved: Literal[True]
    plan_id: str = Field(min_length=1, max_length=128)


class Discover(Strict):
    type: Literal["discover_external"]
    consent: Literal[True]


class SelectExternal(Strict):
    type: Literal["select_external"]
    candidate_index: int = Field(ge=0, le=4)


class ApproveFile(Strict):
    type: Literal["approve_external_file"]
    approved: Literal[True]
    url: str = Field(min_length=1, max_length=2048)


class ConfirmExternal(Strict):
    type: Literal["confirm_external"]
    confirmed: Literal[True]


class RegisterProduct(Strict):
    type: Literal["register_product"]
    name: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=10, max_length=1000)
    geography: str = Field(min_length=2, max_length=160)
    measure: str = Field(min_length=2, max_length=160)
    measure_column: str = Field(min_length=1, max_length=100)
    unit: str = Field(min_length=1, max_length=160)
    coverage_column: str = Field(min_length=1, max_length=100)
    unique_key: list[str] = Field(min_length=1, max_length=20)
    approved: Literal[True]
    rights_reviewed: Literal[True]


class Simple(Strict):
    type: Literal["recover_local", "none_fit", "choose_again", "retry", "propose_registration"]


class ActionRequest(Strict):
    request_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    action: Annotated[Select | Confirm | Approve | Discover | SelectExternal | ApproveFile | ConfirmExternal | RegisterProduct | Simple, Field(discriminator="type")]


def revision_of(items: list[dict]) -> str:
    # Includes metadata, schema, snapshot identity and versions, not sample rows.
    return hashlib.sha256(json.dumps([(p["id"], sql_runs.fingerprint(p)) for p in sorted(items, key=lambda x: x["id"])]).encode()).hexdigest()


def allowed(state: State) -> list[str]:
    stage = state.get("stage")
    if not state.get("catalog_fingerprint"):
        return ["retry"] if stage == "error" else []
    actions = ["select_source"]
    if state.get("selected"):
        actions.append("choose_again")
    if stage == "source_review":
        actions += ["confirm_source"]
    if stage == "sql_review":
        actions += ["approve_sql"]
    if (stage in ("external_review", "external_file_review") or
            (stage == "error" and state.get("retry_node") in ("external_recommender", "external_page", "external_acquire"))) and state.get("external"):
        actions += ["select_external"]
    if (stage == "external_file_review" or
            (stage == "error" and state.get("retry_node") == "external_acquire")) and state.get("external_links"):
        actions += ["approve_external_file"]
    if stage == "external_snapshot_review":
        actions += ["confirm_external"]
    meta = state.get("external_file")
    if stage == "results" and meta and (state.get("result") or {}).get("outcome") == "answered":
        from urllib.parse import urlsplit
        if meta["start"] != "Unknown" and meta["end"] != "Unknown" and not urlsplit(meta["url"]).query and not urlsplit(meta["url"]).fragment:
            actions += ["propose_registration"]
    if stage == "registration_review":
        actions += ["register_product"]
    if stage == "sql_abstain":
        actions += ["recover_local"]
    if stage == "error" and state.get("retry_node") and (state.get("retry_node") != "external_acquire" or state.get("approved_external_url")):
        actions += ["retry"]
    if stage in ("recommendations", "source_review", "sql_abstain", "external_review", "no_local_fit"):
        actions += ["none_fit"]
    if stage in ("external_offer", "external_review") or (stage == "no_local_fit" and state.get("advice", {}).get("limitation") == "data_gap"):
        actions += ["discover_external"]
    return actions


def trace(state: State, stage: str, status: str, result: str, start: float, **extra) -> list[dict]:
    return [*state.get("trace", []), {"stage": stage, "status": status, "result": result,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1), **extra}]


def catalog_node(state: State):
    check_active(state)
    items = catalog()
    if state.get("selected"):
        source = product(state["selected"]["id"])
        return {"catalog_fingerprint": revision_of(items), "selected_fingerprint": sql_runs.fingerprint(source), "stage": "source_review"}
    return {"catalog_fingerprint": revision_of(items)}


def external_item(state: State) -> dict:
    meta = state["external_file"]
    path = (EXTERNAL_BASE / state["run_id"] / "snapshot.parquet").resolve()
    if not path.is_relative_to(EXTERNAL_BASE.resolve()) or not path.is_file():
        raise HTTPException(409, "Temporary external snapshot is unavailable. Start a new analysis.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
        raise HTTPException(409, "Temporary external snapshot changed. Start a new analysis.")
    return {"id": "external_" + state["run_id"].replace("-", ""), "version": 1,
        "name": "Temporary external CSV", "description": "User-approved public CSV for one-off analysis; not a catalog product.",
        "business_context": "One-off source; verify geography, measure and units in the rows before interpreting results.",
        "facets": {"unit": meta["units_observed"][0] if len(meta.get("units_observed", [])) == 1 else "Varies or unknown; inspect source columns", "frequency": "Source CSV"},
        "source": {"url": meta["url"], "name": meta["url"].split("/")[2]},
        "snapshot": {"start": meta["start"], "end": meta["end"], "rows": meta["rows"],
                     "retrieved_at": meta["retrieved_at"], "snapshot_sha256": meta["sha256"]},
        "tables": [{"id": "external_data", "path": str(path.relative_to(ROOT)), "columns": meta["columns"]}],
        "temporary": True}


def check_active(state: State):
    if state.get("run_id"):
        with sqlite3.connect(DATABASE, timeout=10) as conn:
            row = conn.execute("SELECT status FROM workflow_runs WHERE id = ?", [state["run_id"]]).fetchone()
        if not row or row[0] == "cancelled":
            raise HTTPException(409, "Workflow cancelled; results discarded.")


def check_current(state: State):
    check_active(state)
    if state.get("catalog_fingerprint") and revision_of(catalog()) != state["catalog_fingerprint"]:
        raise HTTPException(409, "Catalog changed. Start a new analysis and confirm the source again.")
    if state.get("selected"):
        if state.get("external_file"):
            source = external_item(state)
        else:
            source = product(state["selected"]["id"])
        if source["version"] != state["selected"]["version"] or sql_runs.fingerprint(source) != state.get("selected_fingerprint"):
            raise HTTPException(409, "Selected product changed. Start a new analysis.")


def specialist(state: State, node: str, operation):
    started = time.perf_counter()
    try:
        check_current(state)
        updates, result, extra = operation()
        check_current(state)
        return {**updates, "error": None, "retry_node": None,
                "trace": trace(state, node, "completed", result, started, **extra)}
    except HTTPException as exc:
        if exc.status_code == 409:
            raise
        return {"stage": "error", "error": str(exc.detail), "retry_node": node,
                "trace": trace(state, node, "failed", str(exc.detail), started)}
    except (AdviceError, DiscoveryError, ExternalAdviceError, ExternalFileError) as exc:
        return {"stage": "error", "error": str(exc), "retry_node": node,
                "trace": trace(state, node, "failed", str(exc), started)}


def advisor_node(state: State):
    def call():
        metadata = [public_product(p) for p in catalog()]
        recovery = {"rejected_ids": state.get("rejected", []), "reason": state.get("recovery_reason", "")}
        advice, model, usage = advise(state["question"], metadata, recovery)
        data = validate_advice(advice, metadata, state.get("rejected", []))
        stage = {"recommend": "recommendations", "clarify": "clarification", "no_local_fit": "no_local_fit"}[advice.outcome]
        return {"advice": data, "stage": stage}, advice.outcome, {"model": model, "usage": usage}
    return specialist(state, "advisor", call)


def planner_node(state: State):
    def call():
        source = state["selected"]
        plan = (sql_runs.generate_for_item(state["question"], external_item(state)) if state.get("external_file")
                else sql_runs.generate_saved_plan(sql_runs.Generate(question=state["question"], product_id=source["id"], manifest_version=source["version"], confirmed=True)))
        stage = {"ready": "sql_review", "clarify": "sql_clarification", "abstain": "sql_abstain"}[plan["outcome"]]
        return {"plan": plan, "stage": stage}, plan["outcome"], {"model": plan["model"], "usage": token_usage(plan.get("usage", {}))}
    if not state.get("confirmed"):
        raise HTTPException(409, "Source confirmation required.")
    return specialist(state, "planner", call)


def token_usage(usage):
    return {k: v for k, v in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens") and type(v) is int}


def executor_node(state: State):
    def call():
        result = sql_runs.execute_saved_plan(sql_runs.Execute(run_id=state["plan"]["run_id"], approved=True))
        if state.get("external_file"):
            rows = result["rows"]
            if len(rows) == 1 and not result["truncated"]:
                observations = "; ".join(f"{name.replace('_', ' ')}: {value}" for name, value in rows[0].items())
                result["answer"] = f"From the approved external CSV, {observations}. Check source units and aggregation basis below."
            elif rows:
                result["answer"] = f"The approved external CSV returned {len(rows)} rows. Review the dated observations and units below."
            result["source_url"] = state["external_file"]["url"]
            result["snapshot_date"] = state["external_file"]["retrieved_at"]
        return {"result": result, "stage": "results"}, result["outcome"], {}
    if not state.get("approved") or not state.get("confirmed"):
        raise HTTPException(409, "SQL approval and source confirmation required.")
    return specialist(state, "executor", call)


def discovery_node(state: State):
    def call():
        candidates = ExaProvider().search(state["question"])
        return {"external": [c.model_dump() for c in candidates], "external_advice": None,
                "stage": "external_recommending" if candidates else "external_review"}, f"{len(candidates)} candidates; none acquired", {"provider": "exa"}
    if state.get("command", {}).get("consent") is not True:
        raise HTTPException(409, "External-search consent required.")
    return specialist(state, "discovery", call)


def external_recommender_node(state: State):
    def call():
        advice, model, usage = recommend_external(state["question"], state["external"])
        return {"external_advice": advice, "stage": "external_review"}, advice["outcome"], {"model": model, "usage": usage}
    return specialist(state, "external_recommender", call)


def external_page_node(state: State):
    def call():
        candidate = state["external"][state["external_index"]]
        links = inspect_page(candidate["url"], state["question"])
        return {"external_links": links, "stage": "external_file_review"}, f"{len(links)} CSV/ZIP links", {}
    return specialist(state, "external_page", call)


def external_acquire_node(state: State):
    def call():
        meta = acquire(state["run_id"], state["approved_external_url"])
        item = external_item({**state, "external_file": meta})
        return {"external_file": meta, "selected": public_product(item), "selected_fingerprint": sql_runs.fingerprint(item),
                "stage": "external_snapshot_review"}, f"{meta['rows']} rows validated", {}
    return specialist(state, "external_acquire", call)


def registration_node(state: State):
    started = time.perf_counter()
    try:
        check_current(state)
        result = register_external(state["run_id"], state["external_file"], state["command"])
        return {"registration": {**state["registration"], **result}, "catalog_fingerprint": revision_of(catalog()),
                "stage": "registered", "error": None,
                "trace": trace(state, "registration", "completed", result["product_id"], started)}
    except ExternalFileError as exc:
        return {"stage": "registration_review", "error": str(exc),
                "trace": trace(state, "registration", "failed", str(exc), started)}
    except ValueError:
        message = "Registration validation failed. Review the source schema, coverage and unique observation key."
        return {"stage": "registration_review", "error": message,
                "trace": trace(state, "registration", "failed", message, started)}
    except Exception:
        return {"stage": "registration_review", "error": "Registration failed; the source remains a temporary one-off result.",
                "trace": trace(state, "registration", "failed", "Registration failed", started)}


def human_gate(state: State):
    # No specialist calls or writes before interrupt: this node restarts on resume.
    action = interrupt({"stage": state["stage"], "allowed_actions": allowed(state)})
    return {"command": action}


def dispatch(state: State):
    action = state["command"]
    kind = action["type"]
    cleared = {"plan": None, "result": None, "external": None, "external_advice": None,
               "external_links": None, "external_index": None, "approved_external_url": None, "external_file": None, "registration": None,
               "confirmed": False, "approved": False, "error": None, "retry_node": None}
    if kind == "select_source":
        p = product(action["product_id"])
        return Command(update={**cleared, "selected": public_product(p), "selected_fingerprint": sql_runs.fingerprint(p), "stage": "source_review"}, goto="settle")
    if kind == "choose_again":
        return Command(update={**cleared, "selected": None, "selected_fingerprint": None,
                               "stage": "recommendations" if (state.get("advice") or {}).get("outcome") == "recommend" else "source_choice"}, goto="settle")
    if kind == "confirm_source":
        return Command(update={**cleared, "confirmed": True}, goto="planner")
    if kind == "approve_sql":
        return Command(update={"approved": True, "error": None}, goto="executor")
    if kind == "recover_local":
        rejected = list(dict.fromkeys([*state.get("rejected", []), state["selected"]["id"]]))
        return Command(update={**cleared, "selected": None, "selected_fingerprint": None, "advice": None,
                               "rejected": rejected, "recovery_reason": state["plan"]["reason"]}, goto="advisor")
    if kind == "none_fit":
        return Command(update={**cleared, "selected": None, "selected_fingerprint": None, "stage": "external_offer"}, goto="settle")
    if kind == "discover_external":
        return Command(update={**cleared, "selected": None, "selected_fingerprint": None}, goto="discovery")
    if kind == "select_external":
        return Command(update={"external_index": action["candidate_index"], "external_links": None, "approved_external_url": None,
                               "external_file": None, "registration": None, "selected": None, "selected_fingerprint": None,
                               "plan": None, "result": None, "confirmed": False, "approved": False,
                               "error": None, "retry_node": None}, goto="external_page")
    if kind == "approve_external_file":
        return Command(update={"approved_external_url": action["url"], "plan": None, "result": None,
                               "confirmed": False, "approved": False, "error": None, "retry_node": None}, goto="external_acquire")
    if kind == "confirm_external":
        return Command(update={"confirmed": True, "plan": None, "result": None}, goto="planner")
    if kind == "propose_registration":
        return Command(update={"registration": registration_proposal(state["run_id"], state["external_file"]),
                               "stage": "registration_review", "error": None}, goto="settle")
    if kind == "register_product":
        return Command(goto="registration")
    if kind == "retry":
        # Explicit retry after an uncertain process interruption may repeat a call.
        return Command(update={"error": None}, goto=state["retry_node"])
    raise HTTPException(409, "Unsupported workflow action.")


def build_graph(saver):
    builder = StateGraph(State)
    builder.add_node("catalog", catalog_node)
    builder.add_node("advisor", advisor_node)
    builder.add_node("planner", planner_node)
    builder.add_node("executor", executor_node)
    builder.add_node("discovery", discovery_node)
    builder.add_node("external_recommender", external_recommender_node)
    builder.add_node("external_page", external_page_node)
    builder.add_node("external_acquire", external_acquire_node)
    builder.add_node("registration", registration_node)
    builder.add_node("human_gate", human_gate)
    builder.add_node("dispatch", dispatch)
    builder.add_node("settle", lambda state: {})
    builder.add_edge(START, "catalog")
    builder.add_conditional_edges("catalog", lambda state: "settle" if state.get("selected") else "advisor")
    for node in ("advisor", "planner", "executor", "external_recommender", "external_page", "external_acquire", "registration"):
        builder.add_edge(node, "settle")
    builder.add_conditional_edges("discovery", lambda state: "external_recommender" if state.get("stage") == "external_recommending" else "settle")
    builder.add_edge("settle", "human_gate")
    builder.add_edge("human_gate", "dispatch")
    return builder.compile(checkpointer=saver)


@contextmanager
def storage():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY, created REAL NOT NULL, revision INTEGER NOT NULL,
            status TEXT NOT NULL, pending TEXT, create_key TEXT UNIQUE NOT NULL, create_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_actions (
            run_id TEXT NOT NULL, request_id TEXT NOT NULL, body_hash TEXT NOT NULL,
            response TEXT, PRIMARY KEY (run_id, request_id));
    """)
    saver = SqliteSaver(conn)
    try:
        yield conn, saver, build_graph(saver)
    finally:
        conn.close()


def config(run_id):
    return {"configurable": {"thread_id": run_id}, "recursion_limit": 20}


def lock(run_id):
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(DATABASE.parent / f"workflow-{run_id}.lock"), timeout=0)


def prune(conn, saver):
    expired = conn.execute("SELECT id FROM workflow_runs WHERE created < ?", [time.time() - RUN_TTL]).fetchall()
    for row in expired:
        try:
            with lock(row["id"]):
                saver.delete_thread(row["id"])
                remove_external(row["id"])
                with conn:
                    conn.execute("DELETE FROM workflow_actions WHERE run_id = ?", [row["id"]])
                    conn.execute("DELETE FROM workflow_runs WHERE id = ?", [row["id"]])
        except Timeout:
            continue


def row_for(conn, run_id):
    row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", [run_id]).fetchone()
    if row is None:
        raise HTTPException(404, "Workflow not found or expired. Start a new analysis.")
    if row["created"] < time.time() - RUN_TTL:
        raise HTTPException(410, "Workflow expired. Start a new analysis.")
    return row


def mark(conn, run_id, status, pending=None):
    with conn:
        conn.execute("UPDATE workflow_runs SET status = ?, pending = ? WHERE id = ? AND status != 'cancelled'", [status, pending, run_id])


def public_state(conn, graph, run_id):
    row = row_for(conn, run_id)
    state = graph.get_state(config(run_id)).values
    active = row["status"] == "ready"
    terminal = row["status"] in ("cancelled", "invalidated")
    return {"id": run_id, "revision": row["revision"], "status": "running" if row["status"] == "queued" else row["status"],
            "question": state.get("question", ""), "stage": row["status"] if terminal else state.get("stage", "starting"),
            "allowed_actions": allowed(state) if active else [],
            "catalog_fingerprint": state.get("catalog_fingerprint"), "expires_at": row["created"] + RUN_TTL,
            "selected": None if terminal else state.get("selected"), "advice": None if terminal else state.get("advice"),
            "plan": None if terminal else state.get("plan"), "result": None if terminal else state.get("result"),
            "external": None if terminal else state.get("external"),
            "external_advice": None if terminal else state.get("external_advice"),
            "external_links": None if terminal else state.get("external_links"),
            "external_index": None if terminal else state.get("external_index"),
            "external_file": None if terminal else state.get("external_file"),
            "registration": None if terminal else state.get("registration"),
            "confirmed": state.get("confirmed", False) if active else False,
            "approved": state.get("approved", False) if active else False,
            "error": (state.get("error") or "Catalog changed. Start a new analysis.") if row["status"] == "invalidated" else state.get("error"),
            "trace": state.get("trace", [])}


def recover_interrupted(conn, graph, run_id):
    row = row_for(conn, run_id)
    if row["status"] == "queued" and time.time() - row["created"] < 5:
        return  # initial background task has not acquired its run lock yet
    if row["status"] not in ("running", "queued"):
        return
    snapshot = graph.get_state(config(run_id))
    # A completed operation may have checkpointed before its HTTP response was saved.
    # Only safe, deterministic settle/gate nodes are advanced on a read.
    if snapshot.next and all(n in ("settle", "human_gate") for n in snapshot.next):
        if not snapshot.interrupts:
            graph.invoke(None, config(run_id))
    else:
        node = next((n for n in snapshot.next if n in ("catalog", "advisor", "planner", "executor", "discovery", "external_recommender", "external_page", "external_acquire")), row["pending"] or "advisor")
        graph.update_state(config(run_id), {"stage": "error", "error": "The previous operation was interrupted. Its outcome may be uncertain; explicitly retry or choose another source.", "retry_node": node}, as_node="settle")
        graph.invoke(None, config(run_id))  # human_gate only; no provider or execution call
    mark(conn, run_id, "ready")


def validate_action(state, action):
    if action["type"] not in allowed(state):
        raise HTTPException(409, "This action is not allowed at the current workflow stage.")
    if action["type"] == "select_source":
        p = product(action["product_id"])
        if p["version"] != action["manifest_version"]:
            raise HTTPException(409, "Source version changed. Reload the catalog.")
    if action["type"] == "approve_sql" and action["plan_id"] != state["plan"].get("run_id"):
        raise HTTPException(409, "Approval does not match the reviewed SQL plan.")
    if action["type"] == "select_external" and action["candidate_index"] >= len(state.get("external") or []):
        raise HTTPException(409, "External candidate changed. Review the current results.")
    if action["type"] == "approve_external_file" and action["url"] not in {f["url"] for f in state.get("external_links") or []}:
        raise HTTPException(409, "Approve an exact file URL from the reviewed list.")


def request_hash(request):
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


def run_operation(conn, graph, run_id, input_value):
    try:
        graph.invoke(input_value, config(run_id))
        check_current(graph.get_state(config(run_id)).values)
        # Cancellation can be recorded while a bounded provider call is in progress.
        if row_for(conn, run_id)["status"] != "cancelled":
            mark(conn, run_id, "ready")
        else:
            remove_external(run_id)
    except (HTTPException, CatalogError) as exc:
        if row_for(conn, run_id)["status"] != "cancelled":
            graph.update_state(config(run_id), {"error": str(exc.detail) if isinstance(exc, HTTPException) else "Catalog unavailable. Start a new analysis."}, as_node="settle")
            mark(conn, run_id, "invalidated")
        else:
            remove_external(run_id)
        if isinstance(exc, HTTPException) and exc.status_code != 409:
            raise
    except Exception:
        # No provider internals, stack traces, keys or private URLs in public errors.
        graph.update_state(config(run_id), {"stage": "error", "error": "Workflow operation failed. Explicitly retry or choose another source.", "retry_node": row_for(conn, run_id)["pending"] or "advisor"}, as_node="settle")
        graph.invoke(None, config(run_id))
        if row_for(conn, run_id)["status"] != "cancelled":
            mark(conn, run_id, "ready")
        else:
            remove_external(run_id)


def start_background(run_id):
    # Local development worker. A lost process requires explicit retry, never
    # automatic recreation of an uncertain provider call by GET or startup.
    try:
        with FileLock(str(DATABASE.parent / f"workflow-{run_id}.lock"), timeout=10), storage() as (conn, saver, graph):
            if row_for(conn, run_id)["status"] != "queued":
                return
            mark(conn, run_id, "running", "catalog")
            run_operation(conn, graph, run_id, None)
    except (Timeout, HTTPException, sqlite3.Error, OSError):
        return  # a later inspect reports interruption; no implicit retry


@router.post("")
def create(request: Create, background_tasks: BackgroundTasks):
    try:
        with storage() as (conn, saver, graph):
            prune(conn, saver)
            key, digest = str(request.request_id), request_hash(request)
            # Serialize creation retries separately from each graph thread.
            with lock(key):
                existing = conn.execute("SELECT * FROM workflow_runs WHERE create_key = ?", [key]).fetchone()
                if existing:
                    if existing["create_hash"] != digest:
                        raise HTTPException(409, "Request ID was already used for another question.")
                    with lock(existing["id"]):
                        recover_interrupted(conn, graph, existing["id"])
                        return public_state(conn, graph, existing["id"])
                selected = None
                if request.product_id:
                    p = product(request.product_id)
                    if p["version"] != request.manifest_version:
                        raise HTTPException(409, "Source version changed. Reload the catalog.")
                    selected = public_product(p)
                run_id = str(uuid.uuid4())
                with lock(run_id):
                    with conn:
                        conn.execute("INSERT INTO workflow_runs VALUES (?, ?, 1, 'queued', ?, ?, ?)",
                                     [run_id, time.time(), "catalog", key, digest])
                    graph.update_state(config(run_id), {"run_id": run_id, "question": request.question, "selected": selected,
                        "advice": None, "plan": None, "result": None, "external": None, "external_advice": None,
                        "external_links": None, "external_index": None, "approved_external_url": None,
                        "external_file": None, "registration": None, "rejected": [],
                        "confirmed": False, "approved": False, "trace": [], "stage": "starting"}, as_node=START)
                    background_tasks.add_task(start_background, run_id)
                    return public_state(conn, graph, run_id)
    except Timeout:
        raise HTTPException(409, "Workflow request is already in progress. Reload its state before retrying.") from None
    except CatalogError:
        raise HTTPException(503, "Catalog unavailable. No source suitability assessment was made.") from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local workflow storage unavailable.") from None


@router.get("/{run_id}")
def inspect(run_id: uuid.UUID):
    run_id = str(run_id)
    try:
        with storage() as (conn, saver, graph):
            prune(conn, saver)
            row_for(conn, run_id)
            try:
                with lock(run_id):
                    recover_interrupted(conn, graph, run_id)
                    row = row_for(conn, run_id)
                    if row["status"] == "ready":
                        try:
                            check_current(graph.get_state(config(run_id)).values)
                        except (HTTPException, CatalogError):
                            mark(conn, run_id, "invalidated")
            except Timeout:
                pass  # live operation; inspect its checkpoint without resuming it
            return public_state(conn, graph, run_id)
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local workflow storage unavailable.") from None


@router.post("/{run_id}/actions")
def act(run_id: uuid.UUID, request: ActionRequest):
    run_id = str(run_id)
    try:
        with storage() as (conn, saver, graph), lock(run_id):
            prune(conn, saver)
            recover_interrupted(conn, graph, run_id)
            row = row_for(conn, run_id)
            if row["status"] != "ready":
                raise HTTPException(409, "Workflow is not active. Start a new analysis.")
            state = graph.get_state(config(run_id)).values
            try:
                check_current(state)
            except (HTTPException, CatalogError):
                mark(conn, run_id, "invalidated")
                raise HTTPException(409, "Catalog changed. Start a new analysis.") from None
            key, digest = str(request.request_id), request_hash(request)
            previous = conn.execute("SELECT * FROM workflow_actions WHERE run_id = ? AND request_id = ?", [run_id, key]).fetchone()
            if previous:
                if previous["body_hash"] != digest:
                    raise HTTPException(409, "Request ID was already used for a different action.")
                # If its response was lost, return current state. Never rerun the action.
                return json.loads(previous["response"]) if previous["response"] else public_state(conn, graph, run_id)
            if row["revision"] != request.expected_revision:
                raise HTTPException(409, "Workflow changed. Reload before taking another action.")
            action = request.action.model_dump()
            validate_action(state, action)
            pending = {"confirm_source": "planner", "confirm_external": "planner", "approve_sql": "executor", "recover_local": "advisor", "discover_external": "discovery",
                       "select_external": "external_page", "approve_external_file": "external_acquire",
                       "register_product": "registration"}.get(action["type"], state.get("retry_node") or "advisor")
            if action["type"] == "retry" and pending == "discovery":
                # Retry is a renewed explicit user action, not an automatic search.
                action["consent"] = True
            with conn:
                conn.execute("INSERT INTO workflow_actions VALUES (?, ?, ?, NULL)", [run_id, key, digest])
                conn.execute("UPDATE workflow_runs SET revision = revision + 1, status = 'running', pending = ? WHERE id = ?", [pending, run_id])
            run_operation(conn, graph, run_id, Command(resume=action))
            if state.get("external_file") and not graph.get_state(config(run_id)).values.get("external_file"):
                remove_external(run_id)
            response = public_state(conn, graph, run_id)
            with conn:
                conn.execute("UPDATE workflow_actions SET response = ? WHERE run_id = ? AND request_id = ?", [json.dumps(response), run_id, key])
            return response
    except Timeout:
        raise HTTPException(409, "Another action is in progress. Reload before retrying.") from None
    except CatalogError:
        raise HTTPException(409, "Source unavailable. Reload the catalog.") from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local workflow storage unavailable.") from None


@router.delete("/{run_id}")
def cancel(run_id: uuid.UUID):
    run_id = str(run_id)
    try:
        with storage() as (conn, saver, graph):
            prune(conn, saver)
            row = row_for(conn, run_id)
            if row["status"] != "cancelled":
                with conn:
                    conn.execute("UPDATE workflow_runs SET status = 'cancelled', revision = revision + 1 WHERE id = ?", [run_id])
                remove_external(run_id)
            return public_state(conn, graph, run_id)
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local workflow storage unavailable.") from None
