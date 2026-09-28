"""Saved conversation API; workflow checkpoints remain short-lived."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import ExitStack

from fastapi import APIRouter, BackgroundTasks, HTTPException
from filelock import Timeout
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import conversation_store as store, sql_runs, workflows
from .catalog import CatalogError, product
from .external_files import remove as remove_external

router = APIRouter(prefix="/conversations")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewConversation(Strict):
    request_id: uuid.UUID
    product_id: str | None = Field(default=None, max_length=128)
    manifest_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def paired(self):
        if (self.product_id is None) != (self.manifest_version is None):
            raise ValueError("Dataset ID and version must be provided together")
        return self


class NewTurn(Strict):
    request_id: uuid.UUID
    expected_revision: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=1000)


class ResumeUpload(Strict):
    request_id: uuid.UUID
    expected_revision: int = Field(ge=1)


def require(conn, conversation_id: str) -> dict:
    found = store.get(conn, conversation_id)
    if not found:
        raise HTTPException(404, "Conversation not found.")
    return found


def detail(conn, graph, conversation_id: str) -> dict:
    found = require(conn, conversation_id)
    active = None
    run_id = found["active_run_id"]
    if run_id:
        try:
            row = workflows.row_for(conn, run_id)
            if row["status"] != "cancelled":
                try:
                    with workflows.lock(run_id):
                        workflows.recover_interrupted(conn, graph, run_id)
                        if workflows.row_for(conn, run_id)["status"] == "ready":
                            try:
                                workflows.check_current(graph.get_state(workflows.config(run_id)).values)
                            except (HTTPException, CatalogError):
                                workflows.mark(conn, run_id, "invalidated")
                        workflows.sync_conversation(conn, graph.get_state(workflows.config(run_id)).values)
                except Timeout:
                    pass  # Bounded operation is still running; never replay it from a read.
                active = workflows.public_state(conn, graph, run_id)
        except HTTPException:
            store.detach_run(conn, conversation_id, run_id)
            found = require(conn, conversation_id)
    return {"conversation": found, "events": store.events(conn, conversation_id), "run": active}


@router.post("")
def create(request: NewConversation):
    try:
        with workflows.storage() as (conn, _saver, _graph), workflows.lock(str(request.request_id)):
            existing = conn.execute("SELECT * FROM conversations WHERE create_key = ?", [str(request.request_id)]).fetchone()
            if existing:
                if existing["product_id"] != request.product_id or existing["product_version"] != request.manifest_version:
                    raise HTTPException(409, "Request ID was used for a different conversation.")
                return {"conversation": dict(existing), "events": store.events(conn, existing["id"]), "run": None}
            source = None
            if request.product_id:
                source = product(request.product_id)
                if source["version"] != request.manifest_version:
                    raise HTTPException(409, "Dataset version changed. Reload the catalog.")
            found = store.create(conn, source["name"] if source else "New conversation",
                                 source["id"] if source else None,
                                 source["version"] if source else None,
                                 sql_runs.fingerprint(source) if source else None,
                                 create_key=str(request.request_id))
            return {"conversation": found, "events": [], "run": None}
    except CatalogError:
        raise HTTPException(409, "Dataset unavailable. Reload the catalog.") from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Local conversation storage unavailable.") from None


@router.get("")
def list_conversations():
    with workflows.storage() as (conn, saver, _graph):
        workflows.prune(conn, saver)
        return {"conversations": store.list_all(conn)}


@router.get("/{conversation_id}")
def get(conversation_id: uuid.UUID):
    with workflows.storage() as (conn, _saver, graph):
        workflows.prune(conn, _saver)
        return detail(conn, graph, str(conversation_id))


@router.post("/{conversation_id}/turns")
def add_turn(conversation_id: uuid.UUID, request: NewTurn, background_tasks: BackgroundTasks):
    conversation_id = str(conversation_id)
    question = request.question.strip()
    if not question:
        raise HTTPException(422, "Enter a question.")
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    with workflows.storage() as (conn, saver, graph):
        workflows.prune(conn, saver)
        with workflows.lock(conversation_id):
            found = require(conn, conversation_id)
            previous = conn.execute("SELECT * FROM conversation_requests WHERE conversation_id = ? AND request_id = ?",
                                    [conversation_id, str(request.request_id)]).fetchone()
            if previous:
                if previous["body_hash"] != digest:
                    raise HTTPException(409, "Request ID was used for a different question.")
                return detail(conn, graph, conversation_id)
            created = conn.execute("SELECT * FROM workflow_runs WHERE create_key = ? AND conversation_id = ?",
                                   [str(request.request_id), conversation_id]).fetchone()
            if created:
                state = graph.get_state(workflows.config(created["id"])).values
                if state.get("question") != question:
                    raise HTTPException(409, "Request ID was used for a different question.")
                return detail(conn, graph, conversation_id)
            if found["revision"] != request.expected_revision:
                raise HTTPException(409, "Conversation changed. Reload before sending.")
            run_id = found["active_run_id"]
            if run_id:
                try:
                    row = workflows.row_for(conn, run_id)
                except HTTPException:
                    row = None
                if row and row["status"] == "ready":
                    state = graph.get_state(workflows.config(run_id)).values
                    if "ask_question" not in workflows.allowed(state):
                        raise HTTPException(409, "Finish the current source or SQL review before asking another question.")
                    response = workflows.act(uuid.UUID(run_id), workflows.ActionRequest(
                        request_id=request.request_id, expected_revision=row["revision"],
                        action=workflows.AskQuestion(type="ask_question", question=question)))
                    with conn:
                        conn.execute("INSERT INTO conversation_requests VALUES (?, ?, ?, ?)",
                                     [conversation_id, str(request.request_id), digest, json.dumps({"run_id": run_id})])
                        conn.execute("UPDATE conversations SET revision = revision + 1, updated = ? WHERE id = ?",
                                     [time.time(), conversation_id])
                    return {"conversation": require(conn, conversation_id), "events": store.events(conn, conversation_id), "run": response}
                if row and row["status"] not in ("cancelled", "invalidated"):
                    raise HTTPException(409, "Finish the current workflow before sending another question.")
                store.detach_run(conn, conversation_id, run_id)
            if found["upload_sha256"] and not found["product_id"]:
                raise HTTPException(409, "This temporary file expired. Re-upload the same file before another analysis.")
            if found["product_id"]:
                source = product(found["product_id"])
                if source["version"] != found["product_version"] or sql_runs.fingerprint(source) != found["product_fingerprint"]:
                    raise HTTPException(409, "Pinned dataset version changed. Start a new conversation.")
            response = workflows.create(workflows.Create(
                request_id=request.request_id, question=question, conversation_id=uuid.UUID(conversation_id),
                product_id=found["product_id"], manifest_version=found["product_version"]), background_tasks)
            with conn:
                conn.execute("INSERT INTO conversation_requests VALUES (?, ?, ?, ?)",
                             [conversation_id, str(request.request_id), digest, json.dumps({"run_id": response["id"]})])
            return {"conversation": require(conn, conversation_id), "events": store.events(conn, conversation_id), "run": response}


@router.post("/{conversation_id}/resume-upload")
def resume_upload(conversation_id: uuid.UUID, request: ResumeUpload, background_tasks: BackgroundTasks):
    conversation_id = str(conversation_id)
    with workflows.storage() as (conn, saver, graph):
        workflows.prune(conn, saver)
        with workflows.lock(conversation_id):
            found = require(conn, conversation_id)
            created = conn.execute("SELECT id FROM workflow_runs WHERE create_key = ? AND conversation_id = ?",
                                   [str(request.request_id), conversation_id]).fetchone()
            if created:
                return detail(conn, graph, conversation_id)
            if found["revision"] != request.expected_revision:
                raise HTTPException(409, "Conversation changed. Reload before resuming.")
            if not found["upload_sha256"] or found["product_id"]:
                raise HTTPException(409, "This conversation does not have an expired uploaded dataset.")
            if found["active_run_id"]:
                raise HTTPException(409, "This conversation already has an active workflow.")
            first = next((event["payload"]["text"] for event in store.events(conn, conversation_id)
                          if event["kind"] == "question"), "Analyze this uploaded dataset")
            response = workflows.create(workflows.Create(
                request_id=request.request_id, question=first, conversation_id=uuid.UUID(conversation_id),
                upload_pending=True), background_tasks)
            return {"conversation": require(conn, conversation_id), "events": store.events(conn, conversation_id), "run": response}


@router.delete("/{conversation_id}")
def delete(conversation_id: uuid.UUID, request_id: uuid.UUID, expected_revision: int):
    conversation_id = str(conversation_id)
    with workflows.storage() as (conn, saver, _graph), workflows.lock(conversation_id):
        deleted = conn.execute("SELECT * FROM conversation_deletes WHERE id = ?", [conversation_id]).fetchone()
        if deleted:
            if deleted["request_id"] == str(request_id) and deleted["expected_revision"] == expected_revision:
                return {"deleted": True}
            raise HTTPException(404, "Conversation not found.")
        found = require(conn, conversation_id)
        if found["revision"] != expected_revision:
            raise HTTPException(409, "Conversation changed. Reload before deleting.")
        run_ids = [row[0] for row in conn.execute("SELECT id FROM workflow_runs WHERE conversation_id = ? ORDER BY id",
                                                 [conversation_id]).fetchall()]
        try:
            with ExitStack() as locks:
                for run_id in run_ids:
                    locks.enter_context(workflows.lock(run_id))
                for run_id in run_ids:
                    remove_external(run_id)
                    saver.delete_thread(run_id)
                    with conn:
                        conn.execute("DELETE FROM workflow_actions WHERE run_id = ?", [run_id])
                        conn.execute("DELETE FROM workflow_runs WHERE id = ?", [run_id])
        except Timeout:
            raise HTTPException(409, "A workflow is still running. Retry deletion after it finishes.") from None
        with conn:
            conn.execute("INSERT INTO conversation_deletes VALUES (?, ?, ?)",
                         [conversation_id, str(request_id), expected_revision])
            conn.execute("DELETE FROM conversation_events WHERE conversation_id = ?", [conversation_id])
            conn.execute("DELETE FROM conversation_requests WHERE conversation_id = ?", [conversation_id])
            conn.execute("DELETE FROM conversations WHERE id = ?", [conversation_id])
    return {"deleted": True}
