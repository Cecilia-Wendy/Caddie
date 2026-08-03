"""Caddie MCP Server.

External agents may read the local career workspace and submit reviewable changes.
They cannot apply changes directly; confirmation stays inside Caddie.
"""
from __future__ import annotations

import hashlib
import functools
import inspect
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import agent_runtime
import context as caddie_context
import db
import retrieval
import telemetry
import vcs


mcp = FastMCP(
    "Caddie Career Workspace",
    instructions=(
        "Caddie is the user's local career information source. Read relevant context before "
        "making claims. External writes must be submitted as proposed changes and require "
        "confirmation in Caddie. Never claim that a proposal has already changed the source document. "
        "When a write tool returns confirmation.message_to_user or confirmation.review_url, show it "
        "to the user verbatim so they can open the exact review package with one click. "
        "When the user asks to save content into a job preparation folder, use "
        "propose_track_knowledge_document rather than create_draft_asset."
    ),
)

PROTOCOL_VERSION = "1.2"
_tracking_state = threading.local()


def _confirmation_receipt(task_id: int, change_id: int | None = None,
                          pending_count: int | None = None) -> dict:
    """Return an actionable handoff instead of a bare confirmation flag."""
    base_url = (os.environ.get("CADDIE_APP_URL") or "http://127.0.0.1:8766").rstrip("/")
    review_url = f"{base_url}/?review_task={task_id}"
    if change_id:
        review_url += f"&review_change={change_id}"
    count = pending_count if pending_count is not None else 1
    return {
        "required": True,
        "pending_count": count,
        "review_url": review_url,
        "location": "Caddie > Agent 更新 > 当前任务",
        "next_action": f"请点击链接审核并确认 {count} 项候选更新。",
        "message_to_user": f"我已把结果送入 Caddie，还有 {count} 项需要你确认：{review_url}",
    }


def _agent_family(agent_key: str | None) -> str:
    value = (agent_key or "").lower()
    if "codex" in value:
        return "codex"
    if "claude" in value:
        return "claude"
    if "qoder" in value:
        return "qoder"
    return "other"


def _result_count(result: object) -> int:
    if not isinstance(result, dict):
        return 0
    for key in ("pending_changes", "items", "changes", "events", "jobs"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, int):
            return value
    return 0


def _track_external_tool(tool_name: str, operation_class: str):
    """Measure direct MCP/CLI use without recording arguments or returned content."""
    def decorator(func):
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            if getattr(_tracking_state, "depth", 0):
                return func(*args, **kwargs)
            _tracking_state.depth = 1
            started = time.monotonic()
            try:
                bound = signature.bind_partial(*args, **kwargs)
                agent_key = bound.arguments.get("agent_key")
            except TypeError:
                agent_key = None
            properties = {
                "client_type": os.environ.get("CADDIE_EXTERNAL_TRANSPORT", "mcp"),
                "agent_family": _agent_family(agent_key or os.environ.get("CADDIE_AGENT_KEY")),
                "tool_name": tool_name,
                "operation_class": operation_class,
            }
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                properties.update({
                    "status": "failed",
                    "duration_bucket": telemetry.duration_bucket((time.monotonic() - started) * 1000),
                    "result_count_bucket": "none",
                    "error_type": telemetry.classify_ai_error(str(exc)),
                })
                try:
                    telemetry.log_event("external_agent_tool_completed", properties)
                except Exception:
                    pass
                raise
            finally:
                _tracking_state.depth = 0
            if isinstance(result, dict) and result.get("requires_confirmation"):
                task_id = result.get("task_id")
                if task_id:
                    result.setdefault(
                        "confirmation",
                        _confirmation_receipt(int(task_id), result.get("change_id")),
                    )
            properties.update({
                "status": "success",
                "duration_bucket": telemetry.duration_bucket((time.monotonic() - started) * 1000),
                "result_count_bucket": telemetry.count_bucket(_result_count(result)),
                "error_type": "none",
            })
            try:
                telemetry.log_event("external_agent_tool_completed", properties)
            except Exception:
                pass
            return result

        return wrapped
    return decorator


def _record_connection(agent_key: str | None = None) -> dict:
    """Leave a local receipt only when an MCP client actually starts Caddie."""
    key = (agent_key or os.environ.get("CADDIE_AGENT_KEY") or "external_agent").strip()
    data_dir = Path(os.environ.get("CADDIE_DATA_DIR") or (Path.home() / ".caddie"))
    path = data_dir / "agent-connections.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        registry = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        registry = {}
    if not isinstance(registry, dict):
        registry = {}
    receipt = {
        "agent_key": key,
        "last_seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "protocol_version": PROTOCOL_VERSION,
        "tool_count": 30,
        "process_id": os.getpid(),
    }
    registry[key] = receipt
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return receipt


try:
    _STARTUP_RECEIPT = _record_connection()
except OSError:
    _STARTUP_RECEIPT = {}


def _clip(value: str | None, limit: int = 30000) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...（内容已截断）"


@mcp.tool()
@_track_external_tool("verify_connection", "connect")
def verify_connection(agent_key: str | None = None) -> dict:
    """Verify that this Agent can call Caddie and refresh its local receipt."""
    receipt = _record_connection(agent_key)
    return {"ok": True, "message": "Caddie connection verified", **receipt}


def _hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _require_task(task_id: int) -> dict:
    task = db.get_agent_task(task_id)
    if not task:
        raise ValueError(f"Agent task {task_id} does not exist")
    return task


def _commit(message: str) -> None:
    """Keep MCP writes on the same local version-history path as the web app."""
    vcs.sync_and_commit(message)


def _external_run_for_task(task_id: int, agent_key: str = "external_agent") -> int:
    task = _require_task(task_id)
    if task.get("status") in {"completed", "failed", "cancelled"}:
        raise ValueError("The selected task can no longer accept external changes")
    for run in reversed(task.get("runs") or []):
        if run.get("run_type") == "external" and run.get("status") in {"queued", "running", "ready"}:
            return run["id"]
    return db.create_agent_run({
        "task_id": task_id, "run_type": "external", "expert_key": agent_key,
        "model_profile": "external", "status": "running",
        "actor_type": "external_agent", "actor_key": agent_key,
    })


def _track_knowledge_folder(track_id: int, folder_key: str) -> dict:
    """Resolve a stable folder key instead of making external Agents guess local IDs."""
    keys = {key: name for key, name, _order in db.DEFAULT_TRACK_KNOWLEDGE_FOLDERS}
    if folder_key not in keys:
        raise ValueError(f"folder_key must be one of: {', '.join(keys)}")
    folders, _created = db.ensure_track_knowledge_folders(track_id)
    folder = next((item for item in folders if item.get("name") == keys[folder_key]), None)
    if not folder:
        raise ValueError("Caddie could not resolve the requested job preparation folder")
    return {"key": folder_key, "name": keys[folder_key], "id": folder["id"]}


def _document(document_type: str, document_id: int) -> dict:
    if document_type == "knowledge_item":
        item = db.get_knowledge_item(document_id)
        if not item:
            raise ValueError("Knowledge document does not exist")
        return {
            "type": document_type, "id": document_id, "title": item.get("title"),
            "content": item.get("content") or "", "scope_type": item.get("scope_type"),
            "domain_key": item.get("domain_key"), "company": item.get("company"),
            "track_id": item.get("track_id"), "topic": item.get("topic"),
            "mastery": item.get("mastery"), "updated_at": item.get("updated_at"),
        }
    if document_type == "project":
        item = db.get_project(document_id)
        if not item:
            raise ValueError("Project document does not exist")
        return {
            "type": document_type, "id": document_id, "title": item.get("name"),
            "content": item.get("document") or "", "one_liner": item.get("one_liner"),
            "technologies": item.get("technologies"), "keywords": item.get("keywords"),
            "experience": item.get("experience"), "updated_at": item.get("updated_at"),
        }
    if document_type == "source":
        item = db.get_source(document_id)
        if not item:
            raise ValueError("Source does not exist")
        return {
            "type": document_type, "id": document_id,
            "title": item.get("title") or item.get("file_name"),
            "content": item.get("content") or "", "summary": item.get("summary"),
            "source_type": item.get("source_type"), "track_id": item.get("track_id"),
            "status": item.get("status"), "updated_at": item.get("updated_at"),
        }
    if document_type == "resume_version":
        item = db.get_resume_version(document_id)
        if not item:
            raise ValueError("Resume version does not exist")
        return {
            "type": document_type, "id": document_id, "title": item.get("version_name"),
            "content": item.get("extracted_text") or "", "track_id": item.get("track_id"),
            "status": item.get("status"), "change_summary": item.get("change_summary"),
            "docx_path": item.get("docx_path"), "pdf_path": item.get("pdf_path"),
            "updated_at": item.get("updated_at"),
        }
    raise ValueError("document_type must be knowledge_item, project, source, or resume_version")


@mcp.tool()
@_track_external_tool("list_jobs", "discover")
def list_jobs(status: str | None = None, company: str | None = None, limit: int = 100) -> dict:
    """List job records. Use this first to resolve the track_id for a company and role."""
    items = db.list_job_tracks()
    if status:
        items = [x for x in items if (x.get("application_status") or x.get("status")) == status]
    if company:
        needle = company.strip().lower()
        items = [x for x in items if needle in (x.get("company") or "").lower()]
    fields = ("id", "company", "role", "target", "application_status", "status", "priority",
              "track_group", "company_type", "company_industry", "job_type", "apply_url",
              "applied_date", "next_interview_at", "updated_at")
    return {"items": [{key: item.get(key) for key in fields} for item in items[:max(1, min(limit, 200))]]}


@mcp.tool()
@_track_external_tool("read_job_context", "read")
def read_job_context(track_id: int, query: str | None = None) -> dict:
    """Read assembled context for one job, including JD, constraints, knowledge, gaps and relevant sources."""
    if not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    result = caddie_context.build_context(
        track_id=track_id, intent="external_agent", query=(query or "").strip() or None,
    )
    return {"track_id": track_id, "context": _clip(result.get("text"), 40000), "refs": result.get("refs") or []}


@mcp.tool()
@_track_external_tool("read_context_package", "read")
def read_context_package(track_id: int | None = None, project_id: int | None = None,
                         document_type: str | None = None, document_id: int | None = None,
                         intent: str = "external_agent", query: str | None = None,
                         detail: str = "brief") -> dict:
    """Read selected Caddie context with object revisions and hard/soft constraints.

    Before proposing a document overwrite, re-read this package and ensure the
    target revision has not changed. Do not treat omitted records as available.
    """
    if track_id and not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    if project_id and not db.get_project(project_id):
        raise ValueError("Project does not exist")
    return caddie_context.build_agent_context_package(
        track_id=track_id, project_id=project_id, document_type=document_type,
        document_id=document_id, intent=intent.strip() or "external_agent",
        query=(query or "").strip() or None, detail=detail,
    )


@mcp.tool()
@_track_external_tool("read_company_context", "read")
def read_company_context(company: str) -> dict:
    """Read all job records and company-shared knowledge for one company."""
    name = company.strip()
    if not name:
        raise ValueError("company is required")
    tracks = [x for x in db.list_job_tracks() if (x.get("company") or "").strip() == name]
    knowledge = db.list_knowledge_items(company=name)
    return {
        "company": name,
        "jobs": [{key: x.get(key) for key in ("id", "role", "target", "status", "application_status",
                  "priority", "job_type", "track_group", "notes", "updated_at")} for x in tracks],
        "shared_knowledge": [{"id": x.get("id"), "title": x.get("title"), "topic": x.get("topic"),
                              "content": _clip(x.get("content"), 8000)} for x in knowledge],
    }


@mcp.tool()
@_track_external_tool("search_caddie", "search")
def search_caddie(query: str, track_id: int | None = None, limit: int = 8) -> dict:
    """Hybrid-search Caddie projects, knowledge, sources, job context and generated results."""
    if not query.strip():
        raise ValueError("query is required")
    items = retrieval.hybrid_search(query.strip(), track_id=track_id, limit=max(1, min(limit, 20)))
    return {"query": query, "track_id": track_id, "items": items}


@mcp.tool()
@_track_external_tool("read_document", "read")
def read_document(document_type: str, document_id: int) -> dict:
    """Read a knowledge document, project, source, or resume version by stable ID."""
    item = _document(document_type, document_id)
    item["content"] = _clip(item.get("content"), 40000)
    return item


@mcp.tool()
@_track_external_tool("get_workspace_map", "discover")
def get_workspace_map(track_id: int | None = None, company: str | None = None,
                      include_documents: bool = True, limit: int = 100) -> dict:
    """Discover Caddie's objects and stable write destinations without guessing IDs.

    Call this after ``workspace_bootstrap``. With a track_id it returns that
    job's folders, documents, assets, resume versions and interview records.
    Without a track_id it returns a compact workspace index for choosing one.
    """
    capped = max(1, min(int(limit or 100), 200))
    projects = db.list_projects()[:capped]
    project_rows = [{
        "id": item.get("id"), "name": item.get("name"),
        "company": item.get("experience_company"),
        "role": item.get("experience_role"), "updated_at": item.get("updated_at"),
    } for item in projects]
    if track_id is None:
        jobs = list_jobs(company=company, limit=capped)["items"]
        return {
            "scope": "workspace",
            "jobs": jobs,
            "projects": project_rows,
            "next_step": "Choose a track_id, then call get_workspace_map(track_id=...) or read_context_package.",
        }

    track = db.get_job_track(track_id)
    if not track:
        raise ValueError("Job does not exist")
    folders = db.list_knowledge_folders(track_id=track_id, scope_type="track")
    folder_key_by_name = {
        name: key for key, name, _order in db.DEFAULT_TRACK_KNOWLEDGE_FOLDERS
    }
    knowledge = [
        item for item in db.list_knowledge_items(
            track_id=track_id, scope_type="track", include_archived=False,
        )
        if item.get("track_id") == track_id
    ]
    documents_by_folder: dict[int | None, list[dict]] = {}
    for item in knowledge:
        documents_by_folder.setdefault(item.get("folder_id"), []).append({
            "id": item.get("id"), "title": item.get("title"),
            "topic": item.get("topic"), "mastery": item.get("mastery"),
            "updated_at": item.get("updated_at"),
        })
    folder_rows = []
    for folder in folders:
        row = {
            "key": folder_key_by_name.get(folder.get("name")),
            "id": folder.get("id"), "name": folder.get("name"),
            "document_count": len(documents_by_folder.get(folder.get("id"), [])),
        }
        if include_documents:
            row["documents"] = documents_by_folder.get(folder.get("id"), [])
        folder_rows.append(row)

    applications = [
        item for item in db.get_applications() if item.get("track_id") == track_id
    ]
    return {
        "scope": "job",
        "job": {key: track.get(key) for key in (
            "id", "company", "role", "target", "status", "priority", "track_group",
            "company_type", "company_industry", "job_type", "updated_at",
        )},
        "write_destinations": [
            {"value": f"job.{row['key']}", "label": row["name"],
             "confirmation_required": True}
            for row in folder_rows if row.get("key")
        ] + [
            {"value": "draft", "label": "通用草稿资产", "confirmation_required": False},
            {"value": "existing_document", "label": "更新现有文档", "confirmation_required": True},
        ],
        "folders": folder_rows,
        "assets": [{
            "id": item.get("id"), "title": item.get("title"),
            "asset_type": item.get("asset_type"), "status": item.get("status"),
            "version": item.get("version"), "updated_at": item.get("updated_at"),
        } for item in db.list_assets(track_id=track_id, limit=capped)],
        "resume_versions": [{
            "id": item.get("id"), "version_name": item.get("version_name"),
            "status": item.get("status"), "updated_at": item.get("updated_at"),
        } for item in db.list_resume_versions(track_id)],
        "applications": [{
            "id": item.get("id"), "status": item.get("status"),
            "applied_date": item.get("applied_date"), "source": item.get("source"),
            "interviews": [{
                "id": interview.get("id"), "round_type": interview.get("round_type"),
                "round_number": interview.get("round_number"),
                "interview_date": interview.get("interview_date"),
            } for interview in item.get("interviews") or []],
        } for item in applications],
        "projects": project_rows,
        "next_step": (
            "Read only the relevant context, then call save_career_document with an explicit destination. "
            "Do not use draft when the user named a job folder."
        ),
    }


@mcp.tool()
@_track_external_tool("create_agent_task", "run")
def create_agent_task(instruction: str, title: str | None = None, track_id: int | None = None,
                      object_type: str | None = None, object_id: int | None = None) -> dict:
    """Create a queued Caddie Agent task. This records work but does not silently modify user data."""
    if not instruction.strip():
        raise ValueError("instruction is required")
    if track_id and not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    route = agent_runtime.coordinate(instruction, object_type=object_type, object_id=object_id, track_id=track_id)
    task_id = db.create_agent_task({
        "task_type": route["task_type"], "title": title or instruction.strip()[:48],
        "instruction": instruction.strip(), "object_type": object_type, "object_id": object_id,
        "track_id": track_id, "assigned_expert": route["expert_key"], "status": "queued",
        "created_by": "mcp", "execution_owner": "external", "actor_type": "external_agent",
        "actor_key": "external_agent", "context_json": json.dumps({
            "routing_reason": route["reason"], "external_agent": True,
        }, ensure_ascii=False),
    })
    db.create_agent_event({
        "task_id": task_id, "event_type": "external", "label": "外部 Agent 创建任务",
        "detail": f"已交给{route['expert'].get('name') or route['expert_key']}，等待执行",
        "status": "done",
    })
    _commit(f"外部 Agent 创建任务：{(title or instruction.strip()[:48])[:60]}")
    return {"task_id": task_id, "status": "queued", "expert": route["expert_key"],
            "reason": route["reason"], "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("append_task_event", "run")
def append_task_event(task_id: int, label: str, detail: str = "", status: str = "done") -> dict:
    """Append an auditable progress event to an existing Caddie task."""
    _require_task(task_id)
    if status not in {"pending", "running", "done", "failed"}:
        raise ValueError("status must be pending, running, done, or failed")
    event_id = db.create_agent_event({
        "task_id": task_id, "event_type": "external", "label": label.strip() or "外部 Agent 进度",
        "detail": detail.strip(), "status": status,
    })
    _commit("外部 Agent 更新任务进度")
    return {"event_id": event_id, "task_id": task_id, "status": status}


@mcp.tool()
@_track_external_tool("propose_document_change", "propose")
def propose_document_change(document_type: str, document_id: int, proposed_content: str,
                            reason: str, proposed_title: str | None = None,
                            task_id: int | None = None) -> dict:
    """Submit a full-document candidate update for user review. Never applies the change directly."""
    if document_type not in {"knowledge_item", "project"}:
        raise ValueError("Only knowledge_item and project documents support proposed updates")
    current = _document(document_type, document_id)
    if not proposed_content.strip():
        raise ValueError("proposed_content is required")
    if task_id is not None:
        task = _require_task(task_id)
        if task.get("status") in {"completed", "failed", "cancelled"}:
            raise ValueError("The selected task can no longer accept proposals")
    else:
        expert_key = "knowledge_coach" if document_type == "knowledge_item" else "experience_detective"
        task_id = db.create_agent_task({
            "task_type": "external_document_edit", "title": f"外部 Agent 修改：{current.get('title') or document_id}",
            "instruction": reason.strip() or "审阅外部 Agent 提交的文档修改",
            "object_type": document_type, "object_id": document_id,
            "track_id": current.get("track_id"), "assigned_expert": expert_key,
            "status": "review", "created_by": "mcp", "execution_owner": "external",
            "actor_type": "external_agent", "actor_key": "external_agent",
            "context_json": json.dumps({"external_agent": True}, ensure_ascii=False),
        })
    run_id = db.create_agent_run({
        "task_id": task_id, "run_type": "external", "expert_key": "external_agent",
        "model_profile": "external", "status": "ready", "actor_type": "external_agent",
        "actor_key": "external_agent",
        "input_json": json.dumps({"document_type": document_type, "document_id": document_id}, ensure_ascii=False),
        "summary": reason.strip(),
    })
    metadata = {"base_hash": _hash(current.get("content")), "source": "mcp",
                "original_title": current.get("title") or ""}
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id,
        "action_type": "update_knowledge" if document_type == "knowledge_item" else "update_project_document",
        "target_type": document_type, "target_id": document_id,
        "proposed_title": (proposed_title or current.get("title") or "").strip(),
        "proposed_content": proposed_content.strip(), "reason": reason.strip(),
        "scope_type": current.get("scope_type"), "status": "pending",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review", result_summary="外部 Agent 已提交候选修改，等待确认")
    db.create_agent_event({
        "task_id": task_id, "run_id": run_id, "event_type": "review",
        "label": "外部 Agent 提交候选修改", "detail": "尚未写入原文，等待用户在 Caddie 中确认",
        "status": "done", "payload_json": json.dumps({"change_id": change_id}, ensure_ascii=False),
    })
    _commit("外部 Agent 提交文档候选修改")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "status": "pending", "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("propose_track_knowledge_document", "propose")
def propose_track_knowledge_document(task_id: int, track_id: int, title: str, body: str,
                                     folder_key: str = "interview", topic: str = "",
                                     mastery: str = "learning", reason: str = "",
                                     evidence: list[dict] | None = None,
                                     agent_key: str = "external_agent") -> dict:
    """Propose a new document in a job's real preparation space.

    `folder_key` is one of company, role, professional, interview, or review.
    This never creates the document immediately: the user confirms it in Caddie,
    then it appears in the matching folder instead of the generic draft-asset area.
    """
    if not title.strip() or not body.strip():
        raise ValueError("title and body are required")
    if not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    if mastery not in {"new", "learning", "familiar", "mastered"}:
        raise ValueError("mastery must be new, learning, familiar, or mastered")
    run_id = _external_run_for_task(task_id, agent_key)
    folder = _track_knowledge_folder(track_id, folder_key)
    metadata = {
        "track_id": track_id, "folder_id": folder["id"], "folder_key": folder["key"],
        "folder_name": folder["name"], "topic": topic.strip(), "mastery": mastery,
        "evidence": evidence or [], "agent_key": agent_key, "source": "mcp",
    }
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "create_track_knowledge",
        "target_type": "knowledge_item", "parent_type": "job_track", "parent_id": track_id,
        "proposed_title": title.strip(), "proposed_content": body.strip(),
        "reason": reason.strip() or f"提议保存到当前岗位的「{folder['name']}」",
        "scope_type": "track", "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review", result_summary="外部 Agent 已提交岗位准备文档，等待确认")
    db.create_agent_event({
        "task_id": task_id, "run_id": run_id, "event_type": "review",
        "label": "提交岗位准备文档", "detail": f"尚未写入「{folder['name']}」，等待用户确认",
        "status": "done", "payload_json": json.dumps({"change_id": change_id, "folder_key": folder_key}, ensure_ascii=False),
    })
    _commit(f"外部 Agent 提交岗位准备文档：{title.strip()[:60]}")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "destination": {"track_id": track_id, "folder_key": folder["key"], "folder_name": folder["name"]},
            "status": "pending", "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("save_career_document", "write")
def save_career_document(title: str, body: str, destination: str,
                         track_id: int | None = None, project_id: int | None = None,
                         document_type: str | None = None, document_id: int | None = None,
                         reason: str = "", task_id: int | None = None,
                         agent_key: str = "external_agent",
                         asset_type: str = "other", topic: str = "",
                         mastery: str = "learning") -> dict:
    """Save or propose one career document through a destination-oriented API.

    Destinations:
    - job.company / job.role / job.professional / job.interview / job.review:
      create a reviewable document in that exact job preparation folder.
    - draft: save a generic editable draft immediately.
    - existing_document: propose an update to an existing knowledge_item or
      project document; document_type and document_id are required.

    If task_id is omitted Caddie creates and tracks the external run
    automatically. This is the preferred write tool for Codex and WorkBuddy.
    """
    if not title.strip() or not body.strip():
        raise ValueError("title and body are required")
    destination = destination.strip().lower()
    folder_destinations = {
        "job.company": "company", "job.role": "role",
        "job.professional": "professional", "job.interview": "interview",
        "job.review": "review",
    }
    if destination == "draft":
        return create_draft_asset(
            title=title, body=body, asset_type=asset_type,
            track_id=track_id, project_id=project_id, task_id=task_id,
            agent_key=agent_key, provenance={"destination": "draft"},
        )
    if destination == "existing_document":
        if document_type not in {"knowledge_item", "project"} or not document_id:
            raise ValueError("existing_document requires document_type and document_id")
        return propose_document_change(
            document_type=document_type, document_id=document_id,
            proposed_content=body, reason=reason or "外部 Agent 提交文档更新",
            proposed_title=title, task_id=task_id,
        )
    folder_key = folder_destinations.get(destination)
    if not folder_key:
        raise ValueError(
            "destination must be job.company, job.role, job.professional, "
            "job.interview, job.review, draft, or existing_document"
        )
    if not track_id:
        raise ValueError(f"{destination} requires track_id")
    if task_id is None:
        run = start_external_run(
            title=f"保存到 Caddie：{title.strip()[:40]}",
            instruction=reason.strip() or f"将文档保存到岗位 {track_id} 的 {destination}",
            agent_key=agent_key, track_id=track_id,
            object_type="job_track", object_id=track_id,
            context={"destination": destination, "auto_created": True},
        )
        task_id = run["task_id"]
    result = propose_track_knowledge_document(
        task_id=task_id, track_id=track_id, title=title, body=body,
        folder_key=folder_key, topic=topic, mastery=mastery,
        reason=reason, evidence=[], agent_key=agent_key,
    )
    result["high_level_destination"] = destination
    return result


def _ensure_external_task(task_id: int | None, track_id: int, title: str,
                          instruction: str, agent_key: str) -> tuple[int, int]:
    if task_id is None:
        created = start_external_run(
            title=title, instruction=instruction, agent_key=agent_key,
            track_id=track_id, object_type="job_track", object_id=track_id,
            context={"transactional_change": True},
        )
        return created["task_id"], created["run_id"]
    return task_id, _external_run_for_task(task_id, agent_key)


@mcp.tool()
@_track_external_tool("propose_job_record_update", "propose")
def propose_job_record_update(track_id: int, changes: dict, reason: str,
                              task_id: int | None = None,
                              agent_key: str = "external_agent") -> dict:
    """Propose structured changes to a job/application record for human confirmation.

    Supported fields: company, role, target, track_status, priority, notes,
    persona, apply_url, application_status, applied_date, source, remark_tag,
    and evaluation. Use this for pipeline administration, not career prose.
    """
    track = db.get_job_track(track_id)
    if not track:
        raise ValueError("Job does not exist")
    allowed = {
        "company", "role", "target", "track_status", "priority", "notes",
        "persona", "apply_url", "application_status", "applied_date",
        "source", "remark_tag", "evaluation",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise ValueError(f"Unsupported job fields: {', '.join(unknown)}")
    clean = {key: value for key, value in changes.items() if value is not None}
    if not clean:
        raise ValueError("changes cannot be empty")
    if clean.get("track_status") not in {None, "active", "paused", "archived"}:
        raise ValueError("track_status must be active, paused, or archived")
    if clean.get("priority") not in {None, "high", "normal", "low"}:
        raise ValueError("priority must be high, normal, or low")
    if clean.get("application_status") not in {
        None, "applied", "screening", "written", "interview", "offer", "rejected",
    }:
        raise ValueError("invalid application_status")
    track_view = next((item for item in db.list_job_tracks() if item.get("id") == track_id), {})
    application = db.get_application(track_view.get("application_id")) if track_view.get("application_id") else None
    task_id, run_id = _ensure_external_task(
        task_id, track_id, f"更新岗位事务：{track.get('company') or ''} · {track.get('role') or ''}",
        reason or "提交岗位记录字段修改", agent_key,
    )
    metadata = {
        "track_id": track_id, "application_id": application.get("id") if application else None,
        "changes": clean, "base_track_updated_at": track.get("updated_at"),
        "base_application_updated_at": application.get("updated_at") if application else None,
        "agent_key": agent_key, "source": "mcp",
    }
    summary = "\n".join(f"- {key}: {value}" for key, value in clean.items())
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "update_job_record",
        "target_type": "job_track", "target_id": track_id,
        "proposed_title": f"更新 {track.get('company') or ''} · {track.get('role') or ''} 岗位记录",
        "proposed_content": summary, "reason": reason.strip() or "外部 Agent 提交岗位事务修改",
        "scope_type": "track", "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review", result_summary="岗位记录修改等待确认")
    _commit("外部 Agent 提交岗位记录修改")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "status": "pending", "requires_confirmation": True, "changes": clean}


@mcp.tool()
@_track_external_tool("propose_interview_change", "propose")
def propose_interview_change(track_id: int, operation: str,
                             interview_date: str | None = None,
                             round_type: str | None = None,
                             interview_id: int | None = None,
                             duration_minutes: int = 60,
                             location: str | None = None,
                             meeting_link: str | None = None,
                             notes: str | None = None,
                             reason: str = "",
                             task_id: int | None = None,
                             agent_key: str = "external_agent") -> dict:
    """Propose scheduling, rescheduling, or removing an interview.

    operation is schedule, update, or delete. Changes only take effect after
    the user confirms them in Caddie.
    """
    track = db.get_job_track(track_id)
    if not track:
        raise ValueError("Job does not exist")
    operation = operation.strip().lower()
    if operation not in {"schedule", "update", "delete"}:
        raise ValueError("operation must be schedule, update, or delete")
    current = None
    if operation in {"update", "delete"}:
        if not interview_id:
            raise ValueError(f"{operation} requires interview_id")
        current = db.get_interview(interview_id)
        if not current or current.get("track_id") != track_id:
            raise ValueError("Interview does not belong to the selected job")
    if operation in {"schedule", "update"} and not (interview_date or "").strip():
        raise ValueError(f"{operation} requires interview_date")
    track_view = next((item for item in db.list_job_tracks() if item.get("id") == track_id), {})
    application_id = track_view.get("application_id")
    task_id, run_id = _ensure_external_task(
        task_id, track_id, f"调整面试安排：{track.get('company') or ''} · {track.get('role') or ''}",
        reason or "提交面试安排修改", agent_key,
    )
    values = {
        "interview_date": (interview_date or "").strip() or None,
        "round_type": (round_type or "").strip() or None,
        "duration_minutes": max(1, min(int(duration_minutes or 60), 1440)),
        "location": (location or "").strip() or None,
        "meeting_link": (meeting_link or "").strip() or None,
        "notes": (notes or "").strip() or None,
    }
    metadata = {
        "track_id": track_id, "application_id": application_id,
        "interview_id": interview_id, "operation": operation, "values": values,
        "base_interview_updated_at": current.get("updated_at") if current else None,
        "agent_key": agent_key, "source": "mcp",
    }
    action_labels = {"schedule": "新增面试", "update": "修改面试时间", "delete": "删除错误面试"}
    content = (
        f"{values.get('round_type') or '面试'} · {values.get('interview_date') or current.get('interview_date')}"
        if operation != "delete"
        else f"删除：{current.get('round_type') or '面试'} · {current.get('interview_date') or '时间未填写'}"
    )
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "change_interview_schedule",
        "target_type": "interview", "target_id": interview_id,
        "parent_type": "job_track", "parent_id": track_id,
        "proposed_title": f"{action_labels[operation]}：{track.get('company') or ''} · {track.get('role') or ''}",
        "proposed_content": content, "reason": reason.strip() or "外部 Agent 提交面试排期修改",
        "scope_type": "track", "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review", result_summary="面试安排修改等待确认")
    _commit("外部 Agent 提交面试安排修改")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "status": "pending", "requires_confirmation": True,
            "operation": operation, "track_id": track_id, "interview_id": interview_id}


@mcp.tool()
@_track_external_tool("workspace_bootstrap", "connect")
def workspace_bootstrap(agent_key: str = "external_agent", task_hint: str = "",
                        track_id: int | None = None, project_id: int | None = None) -> dict:
    """Get Caddie's workspace contract and a change cursor before external work begins."""
    if track_id and not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    if project_id and not db.get_project(project_id):
        raise ValueError("Project does not exist")
    return {
        "workspace": {"product": "Caddie", "mode": "local_career_workspace",
                      "change_cursor": db.latest_workspace_change_cursor()},
        "agent": {"key": agent_key, "write_policy": "draft_or_propose_only"},
        "selected": {"track": db.get_job_track(track_id) if track_id else None,
                     "project": db.get_project(project_id) if project_id else None},
        "capabilities": {
            "read": ["get_workspace_map", "list_jobs", "read_job_context", "read_context_package", "search_caddie", "read_document", "get_changes_since"],
            "write_without_confirmation": ["create_draft_asset", "append_external_run_event"],
            "preferred_write": [
                "save_career_document", "propose_job_record_update",
                "propose_interview_change",
            ],
            "requires_confirmation": [
                "propose_fact", "propose_feedback", "propose_document_change",
                "propose_track_knowledge_document", "propose_job_record_update",
                "propose_interview_change",
            ],
        },
        "rules": [
            "Read the minimum necessary context before making a claim.",
            "Facts, numbers, contribution boundaries and feedback constraints must be proposed, not directly written.",
            "User edits win. Re-read relevant objects through get_changes_since before a later update.",
            "Draft assets are allowed but must retain source provenance.",
            "For a named job preparation destination, submit a track-knowledge proposal; do not substitute a generic draft asset.",
            "After submitting a proposal, always show confirmation.message_to_user and its review_url to the user.",
        ],
        "quick_start": [
            "Call get_workspace_map to resolve stable IDs and destinations.",
            "Call read_context_package with the chosen object and task intent.",
            "Use save_career_document for content; use propose_job_record_update or "
            "propose_interview_change for pipeline status and scheduling.",
            "Tell the user whether the result is a draft or awaiting confirmation.",
            "After later edits, call get_changes_since with the returned cursor before continuing.",
        ],
        "task_hint": task_hint.strip(),
    }


@mcp.tool()
@_track_external_tool("get_changes_since", "status")
def get_changes_since(cursor: int = 0, scope_type: str | None = None,
                      scope_id: int | None = None, limit: int = 100) -> dict:
    """Read append-only Caddie changes since a cursor, so an Agent can respect user edits."""
    items = db.list_workspace_change_events(cursor, scope_type, scope_id, limit)
    return {"items": items, "next_cursor": items[-1]["id"] if items else max(0, cursor),
            "latest_cursor": db.latest_workspace_change_cursor()}


@mcp.tool()
@_track_external_tool("start_external_run", "run")
def start_external_run(title: str, instruction: str, agent_key: str = "external_agent",
                       track_id: int | None = None, object_type: str | None = None,
                       object_id: int | None = None, context: dict | None = None) -> dict:
    """Register an external Codex/Claude run. It can later save drafts or submit reviewable proposals."""
    if not title.strip() or not instruction.strip():
        raise ValueError("title and instruction are required")
    if track_id and not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    route = agent_runtime.coordinate(instruction, object_type=object_type, object_id=object_id, track_id=track_id)
    task_id = db.create_agent_task({
        "task_type": route["task_type"], "title": title.strip(), "instruction": instruction.strip(),
        "object_type": object_type, "object_id": object_id, "track_id": track_id, "status": "active",
        "assigned_expert": route["expert_key"], "created_by": "mcp", "execution_owner": "external",
        "actor_type": "external_agent", "actor_key": agent_key,
        "context_json": json.dumps({"routing_reason": route["reason"], "external_context": context or {}}, ensure_ascii=False),
    })
    run_id = db.create_agent_run({
        "task_id": task_id, "run_type": "external", "expert_key": agent_key, "model_profile": "external",
        "status": "running", "actor_type": "external_agent", "actor_key": agent_key,
        "input_json": json.dumps({"instruction": instruction, "context": context or {}}, ensure_ascii=False),
    })
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "external_started",
                           "label": "外部 Agent 开始工作", "detail": f"来源：{agent_key}", "status": "running"})
    db.create_workspace_change_event({"event_type": "external_run_started", "target_type": "agent_task", "target_id": task_id,
                                      "scope_type": "track" if track_id else "global", "scope_id": track_id,
                                      "actor_type": "external_agent", "actor_key": agent_key,
                                      "summary": f"外部 Agent 开始：{title.strip()}"})
    _commit(f"外部 Agent 开始任务：{title.strip()[:60]}")
    return {"task_id": task_id, "run_id": run_id, "expert": route["expert_key"],
            "change_cursor": db.latest_workspace_change_cursor()}


@mcp.tool()
@_track_external_tool("append_external_run_event", "run")
def append_external_run_event(task_id: int, label: str, detail: str = "", status: str = "done",
                              payload: dict | None = None, agent_key: str = "external_agent") -> dict:
    """Add a progress event to an external run. This is safe and does not change career facts."""
    if status not in {"pending", "running", "done", "failed"}:
        raise ValueError("status must be pending, running, done, or failed")
    run_id = _external_run_for_task(task_id, agent_key)
    event_id = db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "external_progress",
                                      "label": label.strip() or "外部 Agent 进度", "detail": detail.strip(), "status": status,
                                      "payload_json": json.dumps(payload or {}, ensure_ascii=False)})
    _commit("外部 Agent 更新任务进度")
    return {"task_id": task_id, "run_id": run_id, "event_id": event_id}


@mcp.tool()
@_track_external_tool("complete_external_run", "run")
def complete_external_run(task_id: int, summary: str = "") -> dict:
    """Finish an external run. Pending proposals remain in Caddie for user confirmation."""
    task = _require_task(task_id)
    for run in task.get("runs") or []:
        if run.get("run_type") == "external" and run.get("status") in {"queued", "running", "ready"}:
            db.update_agent_run(run["id"], status="completed", summary=summary.strip() or run.get("summary"))
    refreshed = _require_task(task_id)
    pending = [item for item in refreshed.get("changes") or [] if item.get("status") == "pending"]
    db.update_agent_task(task_id, status="review" if pending else "completed", result_summary=summary.strip() or None)
    db.create_agent_event({"task_id": task_id, "event_type": "external_completed", "label": "外部 Agent 已完成",
                           "detail": "有候选修改等待确认" if pending else "未提交需要确认的修改", "status": "done"})
    _commit("外部 Agent 完成任务")
    result = _require_task(task_id)
    if pending:
        result["requires_confirmation"] = True
        result["confirmation"] = _confirmation_receipt(task_id, pending_count=len(pending))
    else:
        result["requires_confirmation"] = False
        result["next_action"] = "任务已完成，没有待确认更新。"
    return result


@mcp.tool()
@_track_external_tool("create_draft_asset", "write")
def create_draft_asset(title: str, body: str, asset_type: str = "other", track_id: int | None = None,
                       project_id: int | None = None, task_id: int | None = None,
                       agent_key: str = "external_agent", provenance: dict | None = None) -> dict:
    """Save a low-risk draft asset. It is visible in Caddie but remains a draft, not a confirmed fact."""
    if not title.strip() or not body.strip():
        raise ValueError("title and body are required")
    if track_id and not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    if project_id and not db.get_project(project_id):
        raise ValueError("Project does not exist")
    if task_id:
        _external_run_for_task(task_id, agent_key)
    asset_id = db.create_asset({
        "asset_type": asset_type, "track_id": track_id, "project_id": project_id,
        "title": title.strip(), "body": body, "status": "draft",
        "provenance_json": json.dumps({"source": "external_agent", "agent_key": agent_key,
                                         "task_id": task_id, **(provenance or {})}, ensure_ascii=False),
    })
    db.create_workspace_change_event({"event_type": "draft_asset_created", "target_type": "asset", "target_id": asset_id,
                                      "scope_type": "track" if track_id else ("project" if project_id else "global"),
                                      "scope_id": track_id or project_id, "actor_type": "external_agent", "actor_key": agent_key,
                                      "summary": f"新增草稿资产：{title.strip()[:60]}"})
    _commit(f"外部 Agent 保存草稿资产：{title.strip()[:60]}")
    return {"asset": db.get_asset(asset_id), "status": "draft"}


@mcp.tool()
@_track_external_tool("propose_fact", "propose")
def propose_fact(task_id: int, subject_type: str, predicate: str, value_text: str,
                 subject_id: int | None = None, scope_type: str = "global", scope_id: int | None = None,
                 confidence: float | None = None, evidence: list[dict] | None = None,
                 reason: str = "", agent_key: str = "external_agent") -> dict:
    """Submit a fact candidate for user confirmation. Never use this to overwrite a source document."""
    if not subject_type.strip() or not predicate.strip() or not value_text.strip():
        raise ValueError("subject_type, predicate and value_text are required")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {"subject_type": subject_type.strip(), "subject_id": subject_id, "scope_type": scope_type,
                "scope_id": scope_id, "predicate": predicate.strip(), "confidence": confidence,
                "evidence": evidence or [], "agent_key": agent_key}
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "create_fact", "target_type": "career_fact",
        "parent_type": subject_type.strip(), "parent_id": subject_id, "proposed_title": predicate.strip(),
        "proposed_content": value_text.strip(), "reason": reason.strip() or "外部 Agent 提交事实候选",
        "scope_type": scope_type, "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交事实候选", "detail": "尚未写入事实库，等待用户确认", "status": "done"})
    _commit("外部 Agent 提交事实候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id, "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("propose_experience", "propose")
def propose_experience(task_id: int, company: str, role: str,
                       start_date: str | None = None, end_date: str | None = None,
                       location: str | None = None, description: str = "",
                       evidence: list[dict] | None = None, reason: str = "",
                       agent_key: str = "external_agent") -> dict:
    """Propose one resume experience. Confirmation creates a real Experience record."""
    if not company.strip() or not role.strip():
        raise ValueError("company and role are required")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {
        "company": company.strip(), "role": role.strip(), "start_date": start_date,
        "end_date": end_date, "location": location, "description": description.strip(),
        "evidence": evidence or [], "agent_key": agent_key,
    }
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "create_experience",
        "target_type": "experience", "proposed_title": f"{company.strip()} · {role.strip()}",
        "proposed_content": description.strip() or "创建经历记录",
        "reason": reason.strip() or "从简历中拆出的经历候选", "scope_type": "global",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交经历候选", "detail": "等待用户确认后写入我的经历", "status": "done"})
    _commit("外部 Agent 提交经历候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id, "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("propose_project", "propose")
def propose_project(task_id: int, experience_id: int, name: str,
                    one_liner: str = "", document: str = "", technologies: str = "",
                    keywords: str = "", evidence: list[dict] | None = None,
                    reason: str = "", agent_key: str = "external_agent") -> dict:
    """Propose a project under an existing experience. Confirmation creates the project."""
    if not db.get_experience(experience_id):
        raise ValueError("Experience does not exist")
    if not name.strip():
        raise ValueError("name is required")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {
        "experience_id": experience_id, "name": name.strip(),
        "one_liner": one_liner.strip(), "document": document.strip(),
        "technologies": technologies.strip(), "keywords": keywords.strip(),
        "evidence": evidence or [], "agent_key": agent_key,
    }
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "create_project",
        "target_type": "project", "parent_type": "experience", "parent_id": experience_id,
        "proposed_title": name.strip(), "proposed_content": document.strip() or one_liner.strip(),
        "reason": reason.strip() or "从求职资料中拆出的项目候选", "scope_type": "experience",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交项目候选", "detail": "等待用户确认后写入对应经历", "status": "done"})
    _commit("外部 Agent 提交项目候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "requires_confirmation": True, "confirmation": _confirmation_receipt(task_id, change_id)}


@mcp.tool()
@_track_external_tool("propose_resume_version", "propose")
def propose_resume_version(task_id: int, track_id: int, version_name: str,
                           docx_path: str | None = None, pdf_path: str | None = None,
                           status: str = "editing", change_summary: str = "",
                           reason: str = "", agent_key: str = "external_agent") -> dict:
    """Propose registering local resume files as a version under one job track."""
    if not db.get_job_track(track_id):
        raise ValueError("Job does not exist")
    if not version_name.strip() or not (docx_path or pdf_path):
        raise ValueError("version_name and at least one resume path are required")
    if status not in {"editing", "submitted", "archived"}:
        raise ValueError("status must be editing, submitted, or archived")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {"track_id": track_id, "version_name": version_name.strip(),
                "docx_path": docx_path, "pdf_path": pdf_path, "status": status,
                "change_summary": change_summary.strip(), "agent_key": agent_key}
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "create_resume_version",
        "target_type": "resume_version", "parent_type": "job_track", "parent_id": track_id,
        "proposed_title": version_name.strip(),
        "proposed_content": change_summary.strip() or (docx_path or pdf_path or ""),
        "reason": reason.strip() or "登记本地简历版本", "scope_type": "track",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    _commit("外部 Agent 提交简历版本候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "requires_confirmation": True, "confirmation": _confirmation_receipt(task_id, change_id)}


@mcp.tool()
@_track_external_tool("propose_project_evidence", "propose")
def propose_project_evidence(task_id: int, project_id: int, paths: list[str],
                             reason: str = "", agent_key: str = "external_agent") -> dict:
    """Propose linking existing local files as evidence for one project."""
    if not db.get_project(project_id):
        raise ValueError("Project does not exist")
    clean_paths = list(dict.fromkeys(str(Path(p).expanduser()) for p in (paths or []) if str(p).strip()))
    if not clean_paths:
        raise ValueError("paths is required")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {"project_id": project_id, "paths": clean_paths, "agent_key": agent_key}
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "link_project_evidence",
        "target_type": "source_link", "parent_type": "project", "parent_id": project_id,
        "proposed_title": f"关联 {len(clean_paths)} 份项目证据",
        "proposed_content": "\n".join(clean_paths),
        "reason": reason.strip() or "保留原始文件作为项目事实依据", "scope_type": "project",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    _commit("外部 Agent 提交项目证据候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id,
            "requires_confirmation": True, "confirmation": _confirmation_receipt(task_id, change_id)}


@mcp.tool()
@_track_external_tool("propose_user_profile", "propose")
def propose_user_profile(task_id: int, changes: dict, evidence: list[dict] | None = None,
                         reason: str = "", agent_key: str = "external_agent") -> dict:
    """Propose structured personal-profile fields extracted from a resume."""
    allowed = {"name", "preferred_name", "email", "phone", "location", "target_roles",
               "target_locations", "education", "bio"}
    clean = {key: value for key, value in (changes or {}).items() if key in allowed}
    if not clean:
        raise ValueError("changes has no supported user-profile fields")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {"changes": clean, "evidence": evidence or [], "agent_key": agent_key}
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "update_user_profile",
        "target_type": "user_profile", "proposed_title": "更新个人资料：" + "、".join(clean.keys()),
        "proposed_content": json.dumps(clean, ensure_ascii=False, indent=2),
        "reason": reason.strip() or "从简历中拆出的个人资料候选", "scope_type": "global",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交个人资料候选", "detail": "等待用户确认后更新个人信息", "status": "done"})
    _commit("外部 Agent 提交个人资料候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id, "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("propose_feedback", "propose")
def propose_feedback(task_id: int, original_text: str, scope: str = "global", scope_id: int | None = None,
                     category: str | None = None, polarity: str | None = None, strength: str | None = None,
                     directive: str | None = None, reason: str = "", agent_key: str = "external_agent") -> dict:
    """Submit a feedback constraint candidate. It becomes active only after Caddie confirms and normalizes it."""
    if not original_text.strip():
        raise ValueError("original_text is required")
    run_id = _external_run_for_task(task_id, agent_key)
    metadata = {"scope": scope, "scope_id": scope_id, "category": category, "polarity": polarity,
                "strength": strength, "directive": directive, "agent_key": agent_key}
    change_id = db.create_proposed_change({
        "task_id": task_id, "run_id": run_id, "action_type": "create_feedback", "target_type": "feedback_note",
        "parent_type": scope, "parent_id": scope_id, "proposed_title": "反馈约束",
        "proposed_content": original_text.strip(), "reason": reason.strip() or "外部 Agent 提交反馈候选",
        "scope_type": scope, "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(task_id, status="review")
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交反馈候选", "detail": "尚未注入生成约束，等待用户确认", "status": "done"})
    _commit("外部 Agent 提交反馈候选")
    return {"task_id": task_id, "run_id": run_id, "change_id": change_id, "requires_confirmation": True}


@mcp.tool()
@_track_external_tool("get_agent_task", "status")
def get_agent_task(task_id: int) -> dict:
    """Read task status, progress events and pending/applied candidate changes."""
    return _require_task(task_id)


@mcp.tool()
@_track_external_tool("read_update_package", "status")
def read_update_package(task_id: int) -> dict:
    """Read one review package including progress, draft assets and confirmation-required changes."""
    task = _require_task(task_id)
    pending = [item for item in task.get("changes") or [] if item.get("status") == "pending"]
    return {
        "task": task,
        "draft_assets": db.list_assets_for_agent_task(task_id),
        "change_cursor": db.latest_workspace_change_cursor(),
        "rule": "Draft assets are editable working material. Pending changes are not confirmed career facts until the user applies them in Caddie.",
        "confirmation": _confirmation_receipt(task_id, pending_count=len(pending)) if pending else None,
    }


@mcp.resource("caddie://jobs/{track_id}")
def job_resource(track_id: str) -> str:
    """A job's assembled Caddie context as Markdown."""
    return read_job_context(int(track_id))["context"]


@mcp.resource("caddie://documents/{document_type}/{document_id}")
def document_resource(document_type: str, document_id: str) -> str:
    """A Caddie document body."""
    return read_document(document_type, int(document_id)).get("content") or ""


def main():
    db.init_db()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
