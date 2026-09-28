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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from filelock import FileLock, Timeout
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import sql_runs
from .catalog import ROOT, CatalogError, catalog, product
from .discovery import DiscoveryError, ExaProvider
from .external_advice import ExternalAdviceError, recommend as recommend_external
from .external_files import ExternalFileError, inspect_page, acquire, acquire_uploaded, remove as remove_external, BASE as EXTERNAL_BASE, DOWNLOAD_LIMIT
from .external_registration import proposal as registration_proposal, register as register_external
from .inspection import public_product
from .source_advice import AdviceError, advise, validate_advice
from . import conversation_store as conversations
from .question_context import InterpretationError, interpret

router = APIRouter(prefix="/workflows")
DATABASE = ROOT / ".local" / "workflows.sqlite3"
RUN_TTL = 86400


class State(TypedDict, total=False):
    run_id: str
    conversation_id: str | None
    upload_pending: bool
    initial_product_pinned: bool
    question: str
    intent: str
    analysis_question: str | None
    interpreted_question: str | None
    interpretation_clarification: str | None
    turn_index: int
    turn_history: list[dict]
    source_question_index: int
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
    conversation_id: uuid.UUID | None = None
    upload_pending: bool = False

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
        if self.upload_pending and (self.conversation_id is None or self.product_id is not None):
            raise ValueError("Pending upload requires an existing unpinned conversation")
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


class AskQuestion(Strict):
    type: Literal["ask_question"]
    question: str = Field(min_length=1, max_length=1000)


class ReviseQuestion(Strict):
    type: Literal["revise_question"]
    question: str = Field(min_length=1, max_length=1000)


class ConfirmInterpretation(Strict):
    type: Literal["confirm_interpretation"]
    confirmed: Literal[True]


class ActionRequest(Strict):
    request_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    action: Annotated[Select | Confirm | Approve | Discover | SelectExternal | ApproveFile | ConfirmExternal | RegisterProduct | AskQuestion | ReviseQuestion | ConfirmInterpretation | Simple, Field(discriminator="type")]


def revision_of(items: list[dict]) -> str:
    # Includes metadata, schema, snapshot identity and versions, not sample rows.
    return hashlib.sha256(json.dumps([(p["id"], sql_runs.fingerprint(p)) for p in sorted(items, key=lambda x: x["id"])]).encode()).hexdigest()


def allowed(state: State) -> list[str]:
    stage = state.get("stage")
    if not state.get("catalog_fingerprint"):
        return ["retry"] if stage == "error" else []
    pinned = bool(state.get("conversation_id") and (state.get("confirmed") or state.get("initial_product_pinned")))
    actions = [] if pinned else ["select_source"]
    if state.get("selected") and not pinned:
        actions.append("choose_again")
    if stage == "source_review":
        actions += ["confirm_source"]
    if stage == "sql_review":
        actions += ["approve_sql"]
    if stage == "question_review":
        actions += ["confirm_interpretation", "ask_question"]
    if stage in ("recommendations", "no_local_fit", "external_offer", "external_review",
                 "external_file_review", "awaiting_upload", "upload_error", "error") and not state.get("confirmed"):
        actions += ["upload_file"]
    if state.get("selected") and state.get("confirmed") and stage in (
            "ready_for_question", "results", "registered", "sql_abstain", "sql_clarification", "question_clarification"):
        actions += ["ask_question"]
    if not state.get("confirmed") and stage in ("clarification", "recommendations", "no_local_fit", "external_offer", "error"):
        actions += ["revise_question"]
    if (stage in ("external_review", "external_file_review") or
            (stage == "error" and state.get("retry_node") in ("external_recommender", "external_page", "external_acquire"))) and state.get("external"):
        actions += ["select_external"]
    if (stage == "external_file_review" or
            (stage == "error" and state.get("retry_node") == "external_acquire")) and state.get("external_links"):
        actions += ["approve_external_file"]
    if stage == "external_snapshot_review":
        actions += ["confirm_external"]
    meta = state.get("external_file")
    if stage == "results" and meta and meta.get("origin") != "upload" and (state.get("result") or {}).get("outcome") == "answered":
        from urllib.parse import urlsplit
        if meta["start"] != "Unknown" and meta["end"] != "Unknown" and not urlsplit(meta["url"]).query and not urlsplit(meta["url"]).fragment:
            actions += ["propose_registration"]
    if stage == "registration_review":
        actions += ["register_product"]
    if stage == "sql_abstain" and not pinned:
        actions += ["recover_local"]
    if stage == "error" and state.get("retry_node") and (state.get("retry_node") != "external_acquire" or state.get("approved_external_url")):
        actions += ["retry"]
    if not pinned and stage in ("recommendations", "source_review", "sql_abstain", "external_review", "no_local_fit"):
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
    if state.get("upload_pending"):
        return {"catalog_fingerprint": revision_of(items), "stage": "awaiting_upload"}
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
    if state.get("catalog_fingerprint") and not state.get("confirmed") and revision_of(catalog()) != state["catalog_fingerprint"]:
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
    except (AdviceError, DiscoveryError, ExternalAdviceError, ExternalFileError, InterpretationError) as exc:
        return {"stage": "error", "error": str(exc), "retry_node": node,
                "trace": trace(state, node, "failed", str(exc), started)}


def advisor_node(state: State):
    def call():
        metadata = [public_product(p) for p in catalog()]
        recovery = {"rejected_ids": state.get("rejected", []), "reason": state.get("recovery_reason", "")}
        advice, model, usage = advise(state["question"], metadata, recovery)
        data = validate_advice(advice, metadata, state.get("rejected", []))
        stage = {"recommend": "recommendations", "clarify": "clarification", "no_local_fit": "no_local_fit"}[advice.outcome]
        return {"advice": data, "intent": advice.intent,
                "analysis_question": state["question"] if advice.intent == "analysis" else None,
                "interpreted_question": state["question"] if advice.intent == "analysis" else None,
                "turn_index": 1 if advice.intent == "analysis" else 0,
                "stage": stage}, advice.outcome, {"model": model, "usage": usage}
    return specialist(state, "advisor", call)


def planner_node(state: State):
    def call():
        source = state["selected"]
        question = state.get("interpreted_question") or state.get("analysis_question") or state["question"]
        plan = (sql_runs.generate_for_item(question, external_item(state)) if state.get("external_file")
                else sql_runs.generate_saved_plan(sql_runs.Generate(question=question, product_id=source["id"], manifest_version=source["version"], confirmed=True)))
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
        history = [*state.get("turn_history", []),
                   {"asked": state.get("analysis_question") or state["question"],
                    "interpreted": state.get("interpreted_question") or state.get("analysis_question") or state["question"]}][-5:]
        return {"result": result, "turn_history": history, "stage": "results"}, result["outcome"], {}
    if not state.get("approved") or not state.get("confirmed"):
        raise HTTPException(409, "SQL approval and source confirmation required.")
    return specialist(state, "executor", call)


def interpretation_node(state: State):
    def call():
        answer, model, usage = interpret(state["analysis_question"], state.get("turn_history", []), state["selected"])
        stage = "question_review" if answer.outcome == "resolved" else "question_clarification"
        return {"interpreted_question": answer.question if answer.outcome == "resolved" else None,
                "stage": stage, "interpretation_clarification": answer.clarification}, answer.outcome, {"model": model, "usage": usage}
    return specialist(state, "interpretation", call)


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


def external_upload_node(state: State):
    started = time.perf_counter()
    staged = EXTERNAL_BASE / state["run_id"] / "incoming.upload"
    try:
        check_current(state)
        raw = staged.read_bytes()
        if hashlib.sha256(raw).hexdigest() != state["command"]["sha256"]:
            raise ExternalFileError("Uploaded bytes changed. Please choose the file again.")
        meta = acquire_uploaded(state["run_id"], state["command"]["filename"], raw,
                                state["command"].get("source_page"))
        item = external_item({**state, "external_file": meta})
        return {"external_file": meta, "selected": public_product(item),
                "selected_fingerprint": sql_runs.fingerprint(item), "stage": "external_snapshot_review",
                "error": None, "retry_node": None,
                "trace": trace(state, "external_upload", "completed", f"{meta['rows']} rows validated", started)}
    except (OSError, ExternalFileError) as exc:
        return {"stage": "upload_error", "error": str(exc), "retry_node": None,
                "trace": trace(state, "external_upload", "failed", str(exc), started)}
    finally:
        staged.unlink(missing_ok=True)


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
        return Command(update={**cleared, "confirmed": True,
                               "stage": "ready_for_question" if state.get("intent") == "source_finding" and not state.get("analysis_question") else state["stage"]},
                       goto="settle" if state.get("intent") == "source_finding" and not state.get("analysis_question") else "planner")
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
    if kind == "upload_file":
        return Command(update={"selected": None, "selected_fingerprint": None, "external_file": None,
                               "plan": None, "result": None, "confirmed": False, "approved": False,
                               "error": None, "retry_node": None}, goto="external_upload")
    if kind == "confirm_external":
        source_only = state.get("intent") == "source_finding" and not state.get("analysis_question")
        return Command(update={"confirmed": True, "plan": None, "result": None,
                               "stage": "ready_for_question" if source_only else state["stage"]},
                       goto="settle" if source_only else "planner")
    if kind == "ask_question":
        question = action["question"].strip()
        return Command(update={"analysis_question": question, "interpreted_question": None,
                               "interpretation_clarification": None, "turn_index": state.get("turn_index", 0) + 1,
                               "plan": None, "result": None, "registration": None,
                               "approved": False, "error": None, "retry_node": None},
                       goto="interpretation")
    if kind == "revise_question":
        question = action["question"].strip()
        return Command(update={**cleared, "question": question, "selected": None, "selected_fingerprint": None,
                               "advice": None, "intent": "source_finding", "analysis_question": None,
                               "interpreted_question": None, "turn_index": 0,
                               "source_question_index": state.get("source_question_index", 1) + 1}, goto="advisor")
    if kind == "confirm_interpretation":
        return Command(update={"error": None}, goto="planner")
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
    builder.add_node("interpretation", interpretation_node)
    builder.add_node("executor", executor_node)
    builder.add_node("discovery", discovery_node)
    builder.add_node("external_recommender", external_recommender_node)
    builder.add_node("external_page", external_page_node)
    builder.add_node("external_acquire", external_acquire_node)
    builder.add_node("external_upload", external_upload_node)
    builder.add_node("registration", registration_node)
    builder.add_node("human_gate", human_gate)
    builder.add_node("dispatch", dispatch)
    builder.add_node("settle", lambda state: {})
    builder.add_edge(START, "catalog")
    builder.add_conditional_edges("catalog", lambda state: "settle" if state.get("selected") or state.get("upload_pending") else "advisor")
    for node in ("advisor", "planner", "interpretation", "executor", "external_recommender", "external_page", "external_acquire", "external_upload", "registration"):
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
    conversations.ensure_schema(conn)
    if "conversation_id" not in {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}:
        conn.execute("ALTER TABLE workflow_runs ADD COLUMN conversation_id TEXT")
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
    expired = conn.execute("SELECT id, conversation_id FROM workflow_runs WHERE created < ?", [time.time() - RUN_TTL]).fetchall()
    for row in expired:
        try:
            with lock(row["id"]):
                if row["conversation_id"]:
                    sync_conversation(conn, build_graph(saver).get_state(config(row["id"])).values)
                saver.delete_thread(row["id"])
                remove_external(row["id"])
                with conn:
                    if row["conversation_id"]:
                        conversations.detach_run(conn, row["conversation_id"], row["id"])
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
            "conversation_id": row["conversation_id"], "question": state.get("question", ""),
            "analysis_question": state.get("analysis_question"), "interpreted_question": state.get("interpreted_question"),
            "interpretation_clarification": state.get("interpretation_clarification"),
            "turn_index": state.get("turn_index", 0), "intent": state.get("intent", "analysis"),
            "stage": row["status"] if terminal else state.get("stage", "starting"),
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


def sync_conversation(conn, state: State) -> None:
    conversation_id = state.get("conversation_id")
    if not conversation_id or not conversations.get(conn, conversation_id):
        return
    run_id = state["run_id"]
    if state.get("intent") == "source_finding":
        conversations.put_event(conn, conversation_id, f"{run_id}:source-question:{state.get('source_question_index', 1)}", "question",
                                {"text": state["question"], "purpose": "source_finding"})
    if state.get("advice"):
        conversations.put_event(conn, conversation_id, f"{run_id}:advice", "source_advice", state["advice"])
    if state.get("external") is not None:
        conversations.put_event(conn, conversation_id, f"{run_id}:external", "external_sources",
                                {"candidates": state["external"], "recommendation": state.get("external_advice")})
    selected = state.get("selected")
    if selected and state.get("confirmed"):
        existing = conversations.get(conn, conversation_id)
        if state.get("external_file"):
            meta = state["external_file"]
            if not existing["product_id"]:
                conversations.pin(conn, conversation_id, upload_sha256=meta.get("upload_sha256") or meta["raw_sha256"])
            source = {"name": selected["name"], "temporary": True, "rows": meta["rows"],
                      "coverage": {"start": meta["start"], "end": meta["end"]},
                      "units_observed": meta["units_observed"], "source_url": meta["url"],
                      "raw_sha256": meta["raw_sha256"], "upload_sha256": meta.get("upload_sha256"),
                      "source_page": meta.get("source_page")}
        else:
            if not existing["product_id"]:
                conversations.pin(conn, conversation_id, product_id=selected["id"],
                                  product_version=selected["version"], fingerprint=state["selected_fingerprint"])
            source = {"name": selected["name"], "temporary": False, "id": selected["id"],
                      "version": selected["version"], "fingerprint": state["selected_fingerprint"],
                      "coverage": selected["coverage"], "source_url": selected["source_url"]}
        conversations.put_event(conn, conversation_id, f"{run_id}:selected", "source", source)
    index = state.get("turn_index", 0)
    asked = state.get("analysis_question")
    if index and asked:
        prefix = f"{run_id}:turn:{index}"
        conversations.put_event(conn, conversation_id, prefix + ":question", "question",
                                {"text": asked, "purpose": "analysis"})
        if state.get("interpreted_question"):
            conversations.put_event(conn, conversation_id, prefix + ":interpreted", "interpretation",
                                    {"question": state["interpreted_question"]})
        if state.get("approved") and state.get("plan"):
            plan = state["plan"]
            conversations.put_event(conn, conversation_id, prefix + ":sql", "approved_sql",
                                    {"sql": plan.get("sql"), "reason": plan.get("reason"),
                                     "assumptions": plan.get("assumptions", []), "result_units": plan.get("result_units", [])})
        if state.get("result"):
            result = state["result"]
            bounded = {**result, "rows": [{key: value[:1000] if isinstance(value, str) else value
                                           for key, value in row.items()} for row in result.get("rows", [])[:sql_runs.ROW_LIMIT]]}
            conversations.put_event(conn, conversation_id, prefix + ":result", "result", bounded)
    if state.get("error"):
        conversations.put_event(conn, conversation_id, f"{run_id}:trace:{len(state.get('trace', []))}:error", "error",
                                {"message": state["error"]})
    if state.get("stage") == "registered" and state.get("registration"):
        registration = state["registration"]
        conversations.put_event(conn, conversation_id, f"{run_id}:registration", "registration",
                                {"product_id": registration.get("product_id"), "name": registration.get("name"),
                                 "status": registration.get("status")})


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
        node = next((n for n in snapshot.next if n in ("catalog", "advisor", "planner", "interpretation", "executor", "discovery", "external_recommender", "external_page", "external_acquire", "external_upload")), row["pending"] or "advisor")
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
        state = graph.get_state(config(run_id)).values
        check_current(state)
        sync_conversation(conn, state)
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
                    conversation_id = str(request.conversation_id) if request.conversation_id else None
                    if conversation_id:
                        conv = conversations.get(conn, conversation_id)
                        if not conv:
                            raise HTTPException(404, "Conversation not found.")
                        if conv["active_run_id"]:
                            raise HTTPException(409, "This conversation already has an active workflow.")
                        if conv["product_id"] and (request.product_id != conv["product_id"] or request.manifest_version != conv["product_version"] or sql_runs.fingerprint(product(conv["product_id"])) != conv["product_fingerprint"]):
                            raise HTTPException(409, "Start a new conversation to choose another dataset.")
                    with conn:
                        conn.execute("INSERT INTO workflow_runs (id, created, revision, status, pending, create_key, create_hash, conversation_id) "
                                     "VALUES (?, ?, 1, 'queued', ?, ?, ?, ?)",
                                     [run_id, time.time(), "catalog", key, digest, conversation_id])
                    if conversation_id:
                        conversations.attach_run(conn, conversation_id, run_id,
                                                 request.question if conv["title"] == "New conversation" else None)
                    graph.update_state(config(run_id), {"run_id": run_id, "question": request.question, "selected": selected,
                        "upload_pending": request.upload_pending,
                        "initial_product_pinned": bool(conversation_id and conv["product_id"]),
                        "conversation_id": conversation_id, "intent": "analysis" if selected else "source_finding",
                        "analysis_question": request.question if selected else None,
                        "interpreted_question": request.question if selected else None,
                        "interpretation_clarification": None, "turn_index": 1 if selected else 0,
                        "source_question_index": 1,
                        "turn_history": [],
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
                    sync_conversation(conn, graph.get_state(config(run_id)).values)
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
            pending = {"confirm_source": "planner", "confirm_external": "planner", "confirm_interpretation": "planner",
                       "revise_question": "advisor",
                       "ask_question": "interpretation", "approve_sql": "executor", "recover_local": "advisor", "discover_external": "discovery",
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
            sync_conversation(conn, graph.get_state(config(run_id)).values)
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


@router.post("/{run_id}/upload")
async def upload(run_id: uuid.UUID, request: Request, request_id: uuid.UUID, expected_revision: int):
    """Explicit, bounded raw-body upload. Client filenames never become filesystem paths."""
    run_id = str(run_id)
    filename = request.headers.get("x-datascout-filename", "")
    source_page = request.headers.get("x-datascout-source-page") or None
    if not filename or len(filename) > 160 or not filename.lower().endswith((".csv", ".zip")):
        raise HTTPException(422, "Choose one CSV or ZIP file.")
    if source_page and (len(source_page) > 2048 or not source_page.startswith("https://")):
        raise HTTPException(422, "Source page must be a public HTTPS URL.")
    if int(request.headers.get("content-length", "0") or 0) > DOWNLOAD_LIMIT:
        raise HTTPException(413, "Uploaded file exceeds the size limit.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > DOWNLOAD_LIMIT:
            raise HTTPException(413, "Uploaded file exceeds the size limit.")
    digest = hashlib.sha256(raw).hexdigest()
    action = {"type": "upload_file", "filename": filename, "source_page": source_page, "sha256": digest}
    body_hash = hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()
    try:
        with storage() as (conn, saver, graph), lock(run_id):
            prune(conn, saver)
            recover_interrupted(conn, graph, run_id)
            row = row_for(conn, run_id)
            key = str(request_id)
            previous = conn.execute("SELECT * FROM workflow_actions WHERE run_id = ? AND request_id = ?", [run_id, key]).fetchone()
            if previous:
                if previous["body_hash"] != body_hash:
                    raise HTTPException(409, "Request ID was used for a different upload.")
                return json.loads(previous["response"]) if previous["response"] else public_state(conn, graph, run_id)
            if row["revision"] != expected_revision or row["status"] != "ready":
                raise HTTPException(409, "Workflow changed. Reload before uploading.")
            state = graph.get_state(config(run_id)).values
            check_current(state)
            if "upload_file" not in allowed(state):
                raise HTTPException(409, "Upload is not available at this workflow stage.")
            if row["conversation_id"]:
                expected_sha = conversations.get(conn, row["conversation_id"])["upload_sha256"]
                if expected_sha and digest != expected_sha:
                    raise HTTPException(409, "This conversation is pinned to a different uploaded file. Start a new conversation.")
            target = EXTERNAL_BASE / run_id
            target.mkdir(parents=True, exist_ok=True)
            (target / "incoming.upload").write_bytes(raw)
            with conn:
                conn.execute("INSERT INTO workflow_actions VALUES (?, ?, ?, NULL)", [run_id, key, body_hash])
                conn.execute("UPDATE workflow_runs SET revision = revision + 1, status = 'running', pending = 'external_upload' WHERE id = ?", [run_id])
            run_operation(conn, graph, run_id, Command(resume=action))
            response = public_state(conn, graph, run_id)
            with conn:
                conn.execute("UPDATE workflow_actions SET response = ? WHERE run_id = ? AND request_id = ?",
                             [json.dumps(response), run_id, key])
            return response
    except Timeout:
        raise HTTPException(409, "Another action is in progress. Reload before retrying.") from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local upload storage unavailable.") from None


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
                if row["conversation_id"]:
                    conversations.detach_run(conn, row["conversation_id"], run_id)
            return public_state(conn, graph, run_id)
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local workflow storage unavailable.") from None
