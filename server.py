"""
Caddie - FastAPI 后端
"""
import uuid
import json
import re
import base64
import csv
import io
import os
import sys
import platform
import mimetypes
import hashlib
import difflib
import shlex
import sqlite3
import subprocess
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timedelta
from typing import Literal, Optional
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from starlette.concurrency import run_in_threadpool

import db
import ai
import vcs
import context as caddie_context
import retrieval
import agent_runtime
import harness
import interview_store
import interview_decompose
import speech
import telemetry
import updater
import product_features
from app_version import APP_VERSION, DATABASE_SCHEMA_VERSION

app = FastAPI(title="Caddie", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8766", "http://localhost:8766"],
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Accept", "X-Caddie-Session"],
)

_AI_RATE_WINDOW_SECONDS = 60
_AI_RATE_LIMIT = 30
_ai_rate_events: dict[str, deque] = defaultdict(deque)
_ai_rate_lock = threading.Lock()


def _require_feature_available(feature_key: str):
    feature = product_features.get_feature(feature_key)
    if feature["status"] != "available":
        raise HTTPException(status_code=409, detail=feature["message"])


@app.get("/api/product/features")
def product_feature_registry():
    return {"features": product_features.public_features()}


def _is_expensive_ai_path(path: str, method: str) -> bool:
    if method != "POST":
        return False
    return (
        path == "/api/chat"
        or path.endswith("/diagnose")
        or path.endswith("/jd-analyze")
        or path.endswith("/pitch")
        or path.startswith("/api/interview/mock")
        or path.endswith("/knowledge-agent/runs")
    )


def _telemetry_feature_action(path: str, method: str) -> tuple[str, str] | None:
    """Map write APIs to stable product concepts without inspecting payloads."""
    if method not in {"POST", "PUT", "PATCH", "DELETE"} or not path.startswith("/api/"):
        return None
    if path.startswith(("/api/telemetry", "/api/product-feedback", "/api/update")):
        return None
    rules = (
        ("/api/v2/interview", "interview"),
        ("/api/sources", "sources"),
        ("/api/job-tracks", "job_tracks"),
        ("/api/resume-versions", "resume"),
        ("/api/assets", "assets"),
        ("/api/projects", "projects"),
        ("/api/interview", "interview"),
        ("/api/knowledge", "knowledge"),
        ("/api/worklogs", "worklog"),
        ("/api/workspace-sessions", "agent_workspace"),
        ("/api/sessions", "caddie_chat"),
        ("/api/agent", "agent_workspace"),
        ("/api/chat", "caddie_chat"),
        ("/api/applications", "applications"),
        ("/api/email", "email"),
        ("/api/providers", "ai_settings"),
        ("/api/model-profiles", "ai_settings"),
        ("/api/focus-points", "home_focus"),
    )
    feature = next((label for prefix, label in rules if path.startswith(prefix)), None)
    if not feature:
        return None
    segments = [segment for segment in path.removeprefix("/api/").split("/") if segment]
    semantic = [segment for segment in segments[1:] if not segment.isdigit() and len(segment) < 40]
    if method == "DELETE":
        action = "delete"
    elif method in {"PUT", "PATCH"}:
        action = semantic[-1].replace("-", "_") if semantic else "update"
    else:
        action = semantic[-1].replace("-", "_") if semantic else "create"
    return feature, action


@app.middleware("http")
async def local_request_guard(request, call_next):
    allow_remote = os.environ.get("CADDIE_ALLOW_REMOTE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    raw_host = (request.headers.get("host") or "").lower()
    if raw_host.startswith("["):
        host = raw_host.split("]", 1)[0].strip("[]")
    else:
        host = raw_host.rsplit(":", 1)[0] if raw_host.count(":") == 1 else raw_host
    if not allow_remote and host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return Response("Caddie 仅接受本机请求", status_code=403)
    origin = (request.headers.get("origin") or "").lower()
    if not allow_remote and origin and not (
        origin.startswith("http://127.0.0.1:")
        or origin.startswith("http://localhost:")
        or (origin.startswith("chrome-extension://") and request.url.path.startswith("/api/browser-capture/"))
    ):
        return Response("不允许的请求来源", status_code=403)
    path = request.url.path
    if _is_expensive_ai_path(path, request.method):
        now = time.monotonic()
        with _ai_rate_lock:
            events = _ai_rate_events[path]
            while events and now - events[0] >= _AI_RATE_WINDOW_SECONDS:
                events.popleft()
            if len(events) >= _AI_RATE_LIMIT:
                return Response(
                    "AI 请求过于频繁，请稍后再试",
                    status_code=429,
                    headers={"Retry-After": str(_AI_RATE_WINDOW_SECONDS)},
                )
            events.append(now)
    feature_action = _telemetry_feature_action(path, request.method)
    started = time.monotonic()
    session_id = request.headers.get("x-caddie-session")
    try:
        response = await call_next(request)
    except Exception:
        if feature_action:
            feature, action = feature_action
            telemetry.log_event(
                "feature_action_completed",
                {
                    "feature": feature,
                    "action": action,
                    "status": "failed",
                    "duration_bucket": telemetry.duration_bucket(
                        (time.monotonic() - started) * 1000
                    ),
                    "http_status": 500,
                },
                session_id=session_id,
            )
        raise
    if feature_action:
        feature, action = feature_action
        telemetry.log_event(
            "feature_action_completed",
            {
                "feature": feature,
                "action": action,
                "status": "success" if response.status_code < 400 else "failed",
                "duration_bucket": telemetry.duration_bucket(
                    (time.monotonic() - started) * 1000
                ),
                "http_status": response.status_code,
            },
            session_id=session_id,
        )
    return response

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup():
    db.init_db()
    telemetry.ensure_required_analytics()
    previous_version = _app_setting("app_version")
    _set_app_setting("app_version", APP_VERSION)
    _set_app_setting("database_schema_version", str(DATABASE_SCHEMA_VERSION))
    _set_app_setting("last_migrated_at", datetime.now().astimezone().isoformat(timespec="seconds"))
    vcs.ensure_repo()   # 初始化版本仓库并回填一条初始记录
    linked = db.ensure_application_tracks()
    if linked:
        _commit(f"自动建立并关联 {linked} 条岗位求职线")
    migrated = interview_store.migrate_legacy_documents()
    if migrated:
        _commit(f"迁移 {migrated} 份既有资料到统一文档")
    # Recovery may wait for a busy SQLite writer. Never hold up the local web
    # app startup while durable background work is being resumed.
    threading.Thread(
        target=_recover_interview_decomposition_tasks,
        name="interview-task-recovery",
        daemon=True,
    ).start()
    threading.Thread(
        target=_recover_interview_question_coach_tasks,
        name="interview-question-coach-recovery",
        daemon=True,
    ).start()
    threading.Thread(
        target=_recover_interview_review_tasks,
        name="interview-review-recovery",
        daemon=True,
    ).start()
    threading.Thread(
        target=_recover_interview_growth_tasks,
        name="interview-growth-recovery",
        daemon=True,
    ).start()
    threading.Thread(
        target=_recover_knowledge_coach_tasks,
        name="knowledge-coach-recovery",
        daemon=True,
    ).start()
    telemetry.start_uploader()
    telemetry.log_event(
        "app_started",
        {
            "launch_mode": "server",
            "os_family": platform.system().lower() or "unknown",
            "version_changed": bool(previous_version and previous_version != APP_VERSION),
        },
    )
    _start_email_scheduler()


def _commit(msg: str):
    """每次改动后自动提交一条版本记录。"""
    vcs.sync_and_commit(msg)


def _workspace_event(event_type: str, target_type: str, target_id: int | None,
                     summary: str, scope_type: str = "global", scope_id: int | None = None,
                     actor_type: str = "user", actor_key: str | None = None, payload: dict | None = None):
    """Append a lightweight sync signal without coupling every reader to database internals."""
    return db.create_workspace_change_event({
        "event_type": event_type, "target_type": target_type, "target_id": target_id,
        "scope_type": scope_type, "scope_id": scope_id, "actor_type": actor_type,
        "actor_key": actor_key, "summary": summary,
        "payload_json": json.dumps(payload or {}, ensure_ascii=False),
    })


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


def _source_revision() -> str | None:
    revision = (os.environ.get("CADDIE_BUILD_SHA") or "").strip()
    if revision:
        return revision[:40]
    if getattr(sys, "frozen", False):
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip()[:40] if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@app.get("/api/version")
def version_info():
    return {
        "app_version": APP_VERSION,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
        "source_revision": _source_revision(),
        "packaged": bool(getattr(sys, "frozen", False)),
        "platform": platform.system().lower(),
        "python": platform.python_version(),
    }


@app.get("/api/meta/enums")
def api_enum_contract():
    """Human- and machine-readable legal values for persisted public fields."""
    return {
        "application_status": db.STATUS_FLOW,
        "portfolio_build_status": list(db.PORTFOLIO_STATUS),
        "track_status": ["active", "paused", "archived"],
        "knowledge_mastery": ["new", "learning", "answerable", "mastered"],
        "knowledge_status": ["active", "archived"],
        "document_status": ["draft", "ready", "archived"],
        "document_types": [
            "role_insight", "round_preparation", "answer_sheet", "review_report",
            "next_plan", "self_intro", "project_pitch", "knowledge_card", "other",
        ],
        "opportunity_status": [
            "new", "watching", "shortlisted", "planned", "converted",
            "dismissed", "expired",
        ],
        "opportunity_rule_status": ["unverified", "verified", "conflicting"],
    }


# ─── Schemas ──────────────────────────────────────────────────────────────────

class StrictInputModel(BaseModel):
    """Reject misspelled API fields instead of reporting a false success."""

    class Config:
        extra = "forbid"
        str_strip_whitespace = True


def _validate_iso_date(value: Optional[str], field_label: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label}必须是 YYYY-MM-DD") from exc


class ProviderIn(StrictInputModel):
    id: Optional[str] = None
    name: str
    type: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    capabilities: list[str] = Field(default_factory=list)
    data_policy: str = "unknown"


class ModelProfileIn(BaseModel):
    provider_id: Optional[str] = None
    provider_ids: list[str] = Field(default_factory=list)


class TelemetrySettingsIn(BaseModel):
    enabled: bool
    consent_version: str = telemetry.CONSENT_VERSION


class OnboardingCompleteIn(BaseModel):
    import_method: str = "skip"
    ai_mode: str = "none"


class CourseEntryIn(BaseModel):
    name: str = ""
    grade: str = ""


class HonorEntryIn(BaseModel):
    name: str = ""
    issuer: str = ""
    level: str = ""
    date: str = ""
    description: str = ""


class CampusActivityEntryIn(BaseModel):
    organization: str = ""
    role: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""


class EducationEntryIn(BaseModel):
    institution: str = ""
    degree: str = ""
    major: str = ""
    minor: str = ""
    start_date: str = ""
    end_date: str = ""
    location: str = ""
    gpa: str = ""
    ranking: str = ""
    courses: list[CourseEntryIn] = Field(default_factory=list)
    honors: list[HonorEntryIn] = Field(default_factory=list)
    activities: list[CampusActivityEntryIn] = Field(default_factory=list)


class UserProfileIn(BaseModel):
    name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    target_roles: str = ""
    target_locations: str = ""
    education: list[EducationEntryIn] = Field(default_factory=list)
    bio: str = ""


class ProductFeedbackIn(BaseModel):
    category: str = "other"
    rating: Optional[int] = None
    message: str
    contact: Optional[str] = None


class UpdateChannelIn(BaseModel):
    manifest_url: str = ""


class ChatIn(StrictInputModel):
    message: str = Field(..., max_length=50_000)
    session_id: Optional[str] = None
    track_id: Optional[int] = None
    mode: str = "general"
    attachment_ids: list[int] = Field(default_factory=list)
    local_context_ids: list[int] = Field(default_factory=list)
    local_workspace_run_id: Optional[int] = None
    response_style: Literal["concise", "standard", "deep"] = "standard"
    expert_key: Optional[str] = None
    workspace_mode: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None


class ChatConfirmationResolveIn(StrictInputModel):
    content: str = Field(..., min_length=1, max_length=80_000)


class SessionUpdateIn(BaseModel):
    title: Optional[str] = None
    folder: Optional[str] = None
    is_pinned: Optional[bool] = None


class KnowledgeTopicIn(StrictInputModel):
    topic: str = Field(..., min_length=1, max_length=200)


class ProposeIn(BaseModel):
    session_id: str
    clarification_answers: Optional[list[dict]] = None


class ChatActionProposalIn(BaseModel):
    session_id: str
    track_id: Optional[int] = None


class ChatActionApplyIn(BaseModel):
    session_id: str
    track_id: Optional[int] = None
    actions: list


class FeedbackHarnessIn(BaseModel):
    original_text: str
    scope: Optional[str] = "global"
    scope_id: Optional[int] = None
    category: Optional[str] = None
    polarity: Optional[str] = None
    strength: Optional[str] = None
    directive: Optional[str] = None
    source_id: Optional[int] = None
    apply_stale: bool = True


class HarnessEvaluateIn(BaseModel):
    text: str
    title: Optional[str] = ""
    expert_key: str = "career_lead"
    mode: str = "answer"
    expected_keywords: list[str] = Field(default_factory=list)
    min_score: int = 70


class ProjChatIn(StrictInputModel):
    message: str = Field(..., min_length=1, max_length=50_000)
    mode: Literal["organize", "ask"] = "organize"


class ProjectMessageIn(StrictInputModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=50_000)


class ApplyIn(BaseModel):
    changes: list
    session_id: Optional[str] = None


class ExperienceIn(StrictInputModel):
    company: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=200)
    start_date: Optional[str] = Field(None, max_length=10)
    end_date: Optional[str] = Field(None, max_length=10)
    location: Optional[str] = Field(None, max_length=200)

    @validator("start_date")
    def validate_start_date(cls, value):
        return _validate_iso_date(value, "开始日期")

    @validator("end_date")
    def validate_end_date(cls, value):
        return _validate_iso_date(value, "结束日期")


class ProjectIn(StrictInputModel):
    name: str = Field(..., min_length=1, max_length=240)
    one_liner: Optional[str] = Field(None, max_length=1000)
    document: Optional[str] = Field(None, max_length=300_000)
    technologies: Optional[str] = Field(None, max_length=4000)
    keywords: Optional[str] = Field(None, max_length=4000)


class PortfolioIn(StrictInputModel):
    name: str = Field(..., min_length=1, max_length=240)
    one_liner: Optional[str] = Field(None, max_length=1000)
    document: Optional[str] = Field(None, max_length=300_000)
    technologies: Optional[str] = Field(None, max_length=4000)
    keywords: Optional[str] = Field(None, max_length=4000)
    repo_url: Optional[str] = Field(None, max_length=4000)
    demo_url: Optional[str] = Field(None, max_length=4000)
    cover_source_id: Optional[int] = None
    build_status: Optional[Literal["building", "live", "paused"]] = None


class PortfolioMetaIn(StrictInputModel):
    repo_url: Optional[str] = Field(None, max_length=4000)
    demo_url: Optional[str] = Field(None, max_length=4000)
    cover_source_id: Optional[int] = None
    build_status: Optional[Literal["building", "live", "paused"]] = None


class PortfolioDraftIn(BaseModel):
    github_url: Optional[str] = None
    doc_text: Optional[str] = None


class FocusIngestIn(BaseModel):
    transcript: str
    scope: Optional[str] = "portfolio"


class FocusApplyIn(BaseModel):
    items: list
    scope: Optional[str] = "portfolio"
    paper: Optional[dict] = None
    pass_prediction: Optional[dict] = None
    source_text: Optional[str] = None


class ApplicationIn(StrictInputModel):
    company: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=200)
    industry: Optional[str] = Field(None, max_length=200)
    applied_date: Optional[str] = Field(None, max_length=10)
    status: Literal["active", "applied", "screening", "written", "interview", "offer", "rejected"] = "applied"
    source: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=20_000)
    company_type: Optional[str] = Field(None, max_length=100)
    company_industry: Optional[str] = Field(None, max_length=200)
    job_type: Optional[str] = Field(None, max_length=100)
    apply_url: Optional[str] = Field(None, max_length=4000)
    remark_tag: Optional[str] = Field(None, max_length=100)
    evaluation: Optional[str] = Field(None, max_length=20_000)
    track_id: Optional[int] = None
    job_description: Optional[str] = Field(None, max_length=100_000)

    @validator("applied_date")
    def validate_applied_date(cls, value):
        return _validate_iso_date(value, "投递日期")


class ApplicationStatusIn(StrictInputModel):
    status: Literal["active", "applied", "screening", "written", "interview", "offer", "rejected"]
    allow_skip: Optional[bool] = False


class InterviewScheduleIn(StrictInputModel):
    round_number: Optional[int] = None
    round_type: Optional[str] = None
    interview_date: str = Field(..., min_length=10, max_length=32)
    duration_minutes: Optional[int] = Field(60, ge=1, le=24 * 60)
    location: Optional[str] = Field(None, max_length=500)
    meeting_link: Optional[str] = Field(None, max_length=4000)
    notes: Optional[str] = Field(None, max_length=20_000)
    feedback: Optional[str] = Field(None, max_length=100_000)
    outcome: Optional[str] = Field(None, max_length=100)


class ApplicationMilestoneIn(StrictInputModel):
    event_type: Literal["assessment", "written", "followup", "offer", "rejected", "other"] = "followup"
    title: str = Field(..., min_length=1, max_length=240)
    event_date: str = Field(..., min_length=10, max_length=32)
    notes: Optional[str] = Field(None, max_length=20_000)


class SourceIn(StrictInputModel):
    title: Optional[str] = Field(None, max_length=500)
    source_type: Literal["resume", "jd", "project", "feedback", "worklog", "knowledge", "other"] = "other"
    content: str = Field(..., min_length=1, max_length=1_000_000)


class ProjectLocalSourcesIn(StrictInputModel):
    paths: list[str] = Field(..., min_length=1, max_length=100)


class ApplicationTrackIn(StrictInputModel):
    track_id: Optional[int] = None


class LocalDiscoveryScanIn(BaseModel):
    paths: list[str] = Field(default_factory=list)
    include_extensions: list[str] = Field(default_factory=list)
    include_hidden: bool = False
    max_files: int = 800
    max_file_mb: int = 25
    use_ai: bool = False


class LocalDiscoveryImportIn(BaseModel):
    candidate_ids: list[int] = Field(default_factory=list)
    analyze_after_import: bool = False


class LocalDiscoveryUpdateIn(BaseModel):
    source_type: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None


class JobTrackIn(StrictInputModel):
    company: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=200)
    track_group: Optional[str] = Field(None, max_length=100)
    target: Optional[str] = Field(None, max_length=500)
    jd: Optional[str] = Field(None, max_length=100_000)
    apply_url: Optional[str] = Field(None, max_length=4000)
    company_type: Optional[str] = Field(None, max_length=100)
    company_industry: Optional[str] = Field(None, max_length=200)
    job_type: Optional[str] = Field(None, max_length=100)
    status: Literal["active", "paused", "archived"] = "active"
    notes: Optional[str] = Field(None, max_length=20_000)
    persona: Optional[str] = Field(None, max_length=20_000)
    priority: Literal["high", "normal", "low"] = "normal"


class OpportunityRuleIn(StrictInputModel):
    rule_status: Literal["unverified", "verified", "conflicting"] = "unverified"
    early_batch_impact: Literal["unknown", "safe", "risky", "blocks"] = "unknown"
    locks_choice: Literal["unknown", "no", "yes", "partial"] = "unknown"
    multi_apply_allowed: Literal["unknown", "no", "yes", "partial"] = "unknown"
    cooldown_days: Optional[int] = Field(None, ge=0, le=3650)
    rolling_review: Literal["unknown", "no", "yes", "partial"] = "unknown"
    referral_required: Literal["unknown", "no", "yes", "partial"] = "unknown"
    resume_editable: Literal["unknown", "no", "yes", "partial"] = "unknown"
    assessment_trigger: Optional[str] = Field(None, max_length=2000)
    evidence: Optional[str] = Field(None, max_length=20_000)


class JobOpportunityIn(StrictInputModel):
    company: str = Field(..., min_length=1, max_length=200)
    role: str = Field(..., min_length=1, max_length=200)
    direction: Optional[str] = Field(None, max_length=200)
    company_industry: Optional[str] = Field(None, max_length=200)
    company_type: Optional[str] = Field(None, max_length=100)
    batch_type: Literal["early", "autumn", "supplement", "spring", "intern", "unknown"] = "autumn"
    status: Literal["new", "watching", "shortlisted", "planned", "converted", "dismissed", "expired"] = "new"
    priority: Literal["high", "normal", "low"] = "normal"
    fit_score: int = Field(0, ge=0, le=100)
    source_type: Optional[str] = Field(None, max_length=100)
    source_title: Optional[str] = Field(None, max_length=500)
    source_url: Optional[str] = Field(None, max_length=4000)
    apply_url: Optional[str] = Field(None, max_length=4000)
    location: Optional[str] = Field(None, max_length=200)
    deadline_date: Optional[str] = Field(None, max_length=10)
    deadline_type: Literal["hard", "soft", "rolling", "unknown"] = "hard"
    flow_days: int = Field(4, ge=0, le=365)
    buffer_days: int = Field(1, ge=0, le=365)
    jd: Optional[str] = Field(None, max_length=100_000)
    notes: Optional[str] = Field(None, max_length=20_000)
    evidence: Optional[object] = None
    rules: Optional[OpportunityRuleIn] = None

    @validator("deadline_date")
    def validate_deadline_date(cls, value):
        return _validate_iso_date(value, "截止日期")


class OpportunityPlanItemIn(StrictInputModel):
    title: str = Field(..., min_length=1, max_length=240)
    action_type: Optional[str] = "prepare"
    due_date: Optional[str] = Field(None, max_length=10)
    status: Literal["todo", "doing", "done", "cancelled"] = "todo"
    notes: Optional[str] = Field(None, max_length=20_000)
    calendar_scope: Optional[str] = "opportunity"

    @validator("due_date")
    def validate_due_date(cls, value):
        return _validate_iso_date(value, "计划日期")


class CareerCalendarItemIn(StrictInputModel):
    track_id: Optional[int] = Field(None, gt=0)
    title: str = Field(..., min_length=1, max_length=240)
    item_type: Literal["task", "reminder", "focus", "application_goal", "review"] = "task"
    starts_at: str = Field(..., min_length=10, max_length=32)
    duration_minutes: int = Field(30, ge=5, le=1440)
    color: Literal["blue", "green", "amber", "red", "violet", "slate"] = "green"
    target_count: Optional[int] = Field(None, ge=1, le=100)
    status: Literal["todo", "done", "cancelled"] = "todo"
    notes: Optional[str] = Field(None, max_length=20_000)


class OpportunityParseIn(BaseModel):
    source_type: str = "manual"
    source_title: Optional[str] = None
    source_url: Optional[str] = None
    content: Optional[str] = None


class OpportunityImportIn(BaseModel):
    items: list[dict]


class OpportunitySourceReadIn(BaseModel):
    source_type: str = "xiaohongshu"
    url: str
    title: Optional[str] = None


class TrackGapIn(BaseModel):
    track_id: Optional[int] = None
    dimension: Optional[str] = None
    requirement: Optional[str] = None
    my_status: Optional[str] = "missing"
    severity: Optional[str] = "fixable"
    plan_type: Optional[str] = "none"
    plan_ref_type: Optional[str] = None
    plan_ref_id: Optional[int] = None
    status: Optional[str] = "todo"
    note: Optional[str] = None
    excluded_from_score: Optional[bool] = False
    exclusion_reason: Optional[str] = None


class AssetIn(BaseModel):
    asset_type: str = "other"
    track_id: Optional[int] = None
    project_id: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    status: str = "draft"


class ResumeVersionIn(BaseModel):
    version_name: str
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    status: Optional[str] = "editing"
    change_summary: Optional[str] = None


class SubmissionMaterialIn(StrictInputModel):
    application_id: Optional[int] = None
    material_type: Literal["application_text", "attachment", "email", "proof"] = "application_text"
    title: str = Field(..., min_length=1, max_length=240)
    content: Optional[str] = Field(None, max_length=200_000)
    file_path: Optional[str] = Field(None, max_length=4000)
    external_ref: Optional[str] = Field(None, max_length=4000)
    frozen: bool = True


class CompanySubmissionMaterialIn(StrictInputModel):
    material_type: Literal["application_text", "attachment", "question_bank", "email", "proof"] = "application_text"
    title: str = Field(..., min_length=1, max_length=240)
    content: Optional[str] = Field(None, max_length=200_000)
    file_path: Optional[str] = Field(None, max_length=4000)
    external_ref: Optional[str] = Field(None, max_length=4000)
    change_summary: Optional[str] = Field(None, max_length=1000)


class CompanySubmissionUsageIn(StrictInputModel):
    track_id: Optional[int] = None
    application_id: Optional[int] = None
    usage_title: Optional[str] = Field(None, max_length=240)
    submitted_file_path: Optional[str] = Field(None, max_length=4000)
    submitted_ref: Optional[str] = Field(None, max_length=4000)
    notes: Optional[str] = Field(None, max_length=20_000)
    used_at: Optional[str] = Field(None, max_length=40)


class BrowserCaptureIn(StrictInputModel):
    company: str = Field(..., min_length=1, max_length=200)
    track_id: Optional[int] = None
    title: Optional[str] = Field(None, max_length=500)
    page_url: str = Field(..., min_length=1, max_length=4000)
    portal_host: Optional[str] = Field(None, max_length=500)
    structure: list = Field(default_factory=list)


class BrowserCaptureUpdateIn(StrictInputModel):
    title: Optional[str] = Field(None, max_length=500)
    track_id: Optional[int] = None
    structure: list = Field(default_factory=list)


class KnowledgeItemIn(StrictInputModel):
    title: str = Field(..., min_length=1, max_length=240)
    content: Optional[str] = Field(None, max_length=300_000)
    scope_type: Literal["global", "domain", "company", "track"] = "global"
    domain_key: Optional[str] = None
    company: Optional[str] = None
    track_id: Optional[int] = None
    topic: Optional[str] = None
    mastery: Literal["new", "learning", "answerable", "mastered"] = "learning"
    folder_id: Optional[int] = None
    status: Literal["active", "archived"] = "active"


class KnowledgeFolderIn(BaseModel):
    name: str
    parent_id: Optional[int] = None
    scope_type: Optional[str] = "global"
    domain_key: Optional[str] = None
    company: Optional[str] = None
    track_id: Optional[int] = None
    sort_order: Optional[int] = 0


# ─── Interview system v2 schemas ────────────────────────────────────────────

class DocumentV2In(StrictInputModel):
    title: str = Field(..., min_length=1, max_length=240)
    document_type: str = Field(..., min_length=1, max_length=100)
    body: Optional[str] = Field("", max_length=500_000)
    scope_type: Literal["global", "domain", "company", "track"] = "global"
    track_id: Optional[int] = None
    round_id: Optional[int] = None
    project_id: Optional[int] = None
    folder_key: Optional[str] = None
    source_type: Optional[str] = "manual"
    source_ref_type: Optional[str] = None
    source_ref_id: Optional[int] = None
    editable: bool = True
    locked_source: bool = False
    metadata: Optional[dict] = None
    change_summary: Optional[str] = None


class DocumentV2UpdateIn(StrictInputModel):
    title: Optional[str] = Field(None, min_length=1, max_length=240)
    body: Optional[str] = Field(None, max_length=500_000)
    expected_version: Optional[int] = None
    change_summary: Optional[str] = None


class InterviewRoundV2In(BaseModel):
    round_number: Optional[int] = None
    round_name: Optional[str] = None
    round_type: Optional[str] = None
    interview_mode: Optional[str] = "unknown"
    language: Optional[str] = "zh"
    scheduled_at: Optional[str] = None
    status: Optional[str] = "scheduled"
    notes: Optional[str] = None
    inherited_from_round_id: Optional[int] = None


class RoundDocumentLinkIn(BaseModel):
    document_id: int
    use_type: Optional[str] = "reference"
    purpose: Optional[str] = None


class TranscriptSourceV2In(BaseModel):
    source_kind: str = "paste"
    title: Optional[str] = None
    raw_text: str
    file_path: Optional[str] = None
    locked: bool = True


INTERVIEW_SOURCE_KINDS = {
    "paste",
    "file",
    "asr",
    "hr_feedback",
    "self_note",
    # The review Agent accepts an unclassified bundle first and decides how to
    # organize it afterwards. Keep this distinct from a manually labelled paste
    # so the original intake path remains traceable.
    "agent_intake",
}


class InterviewParticipantV2In(BaseModel):
    participant_key: str
    display_name: Optional[str] = None
    role: str = "unknown"
    organization_role: Optional[str] = None
    confidence: Optional[float] = None
    confirmed: bool = False


class InterviewOutcomeV2In(BaseModel):
    actual_result: str
    result_at: Optional[str] = None
    evidence_type: Optional[str] = "user_confirmed"
    evidence_text: Optional[str] = None
    user_note: Optional[str] = None


class InterviewQuestionClassificationIn(BaseModel):
    question_type: Optional[str] = None
    ability_key: Optional[str] = None
    intent: Optional[str] = None
    tags: list[dict] = []
    links: list[dict] = []
    confirmed: bool = True


class InterviewQuestionAnswerIn(BaseModel):
    body: str


class InterviewQuestionTextIn(StrictInputModel):
    question: str = Field(..., min_length=1, max_length=4000)


class InterviewQuestionAIAnswerIn(BaseModel):
    mode: Literal["recover", "improve", "generate"] = "improve"
    instruction: Optional[str] = None


class InterviewQuestionCoachIn(BaseModel):
    instruction: Optional[str] = None


class InterviewQuestionCoachMessageIn(BaseModel):
    message: str


class InterviewQuestionKnowledgeDraftIn(BaseModel):
    instruction: Optional[str] = None


class InterviewQuestionKnowledgeSaveIn(StrictInputModel):
    title: str = Field(..., min_length=1, max_length=240)
    content: str = Field(..., min_length=1, max_length=300_000)
    topic: Optional[str] = Field(None, max_length=120)
    scope_type: Literal["track", "global"] = "track"


class InterviewGrowthAnalysisIn(StrictInputModel):
    title: Optional[str] = Field(None, max_length=240)
    role_family: str = Field(..., min_length=1, max_length=120)
    role_subtype: Optional[str] = Field(None, max_length=120)
    selected_round_ids: list[int] = []
    target_track_id: Optional[int] = None
    target_jd: Optional[str] = Field(None, max_length=100_000)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    mock_question_count: int = Field(10, ge=5, le=30)


class InterviewGrowthMockMessageIn(StrictInputModel):
    message: str = Field(..., min_length=1, max_length=50_000)
    voice_turn_id: Optional[int] = None


class ReviewActionUpdateIn(BaseModel):
    title: Optional[str] = None
    detail: Optional[str] = None
    priority: Optional[Literal["high", "medium", "low"]] = None
    status: Optional[Literal["proposed", "accepted", "completed", "dismissed"]] = None


@app.get("/api/v2/health/interview-system")
def interview_system_health():
    return {"ok": True, "data": {"schema": "v2", "documents": True, "rounds": True}}


@app.get("/api/v2/documents")
def list_documents_v2(track_id: Optional[int] = None, round_id: Optional[int] = None,
                      document_type: Optional[str] = None, folder_key: Optional[str] = None,
                      project_id: Optional[int] = None):
    return {"ok": True, "items": interview_store.list_documents(
        track_id, round_id, document_type, folder_key, project_id=project_id
    )}


@app.post("/api/v2/documents")
def create_document_v2(body: DocumentV2In):
    if not body.title.strip():
        raise HTTPException(400, "文档标题不能为空")
    did = interview_store.create_document(body.dict())
    _commit(f"新增统一文档：{body.title.strip()}")
    return {"ok": True, "data": interview_store.get_document(did)}


@app.get("/api/v2/documents/{did}")
def get_document_v2(did: int):
    item = interview_store.get_document(did)
    if not item:
        raise HTTPException(404, "文档不存在")
    return {"ok": True, "data": item}


@app.put("/api/v2/documents/{did}")
def update_document_v2(did: int, body: DocumentV2UpdateIn):
    payload = {k: v for k, v in body.dict().items() if k not in {"expected_version"} and v is not None}
    item, error = interview_store.update_document(did, payload, body.expected_version)
    if error == "not_found":
        raise HTTPException(404, "文档不存在")
    if error == "locked":
        raise HTTPException(409, "原始资料已锁定，请编辑整理后的文档")
    if error == "conflict":
        raise HTTPException(409, {"code": "DOCUMENT_VERSION_CONFLICT", "current": item})
    _commit(f"更新统一文档：{item.get('title') or did}")
    return {"ok": True, "data": item}


@app.get("/api/v2/documents/{did}/versions")
def document_versions_v2(did: int):
    if not interview_store.get_document(did):
        raise HTTPException(404, "文档不存在")
    return {"ok": True, "items": interview_store.list_document_versions(did)}


@app.delete("/api/v2/documents/{did}")
def archive_document_v2(did: int):
    if not interview_store.archive_document(did):
        raise HTTPException(404, "文档不存在")
    _commit(f"归档统一文档：{did}")
    return {"ok": True, "status": "archived"}


@app.post("/api/v2/documents/{did}/restore")
def restore_document_v2(did: int):
    if not interview_store.get_document(did, include_archived=True):
        raise HTTPException(404, "文档不存在")
    interview_store.restore_document(did)
    _commit(f"恢复统一文档：{did}")
    return {"ok": True, "data": interview_store.get_document(did)}


@app.get("/api/v2/job-tracks/{tid}/interview-rounds")
def list_interview_rounds_v2(tid: int):
    if not db.get_job_track(tid):
        raise HTTPException(404, "岗位不存在")
    return {"ok": True, "items": interview_store.list_rounds(tid)}


@app.post("/api/v2/job-tracks/{tid}/interview-rounds")
def create_interview_round_v2(tid: int, body: InterviewRoundV2In):
    track = db.get_job_track(tid)
    if not track:
        raise HTTPException(404, "岗位不存在")
    try:
        rid = interview_store.create_round(tid, body.dict())
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(409, "该岗位已有相同轮次编号")
        raise
    interview_store.ensure_round_documents(rid)
    _commit(f"新增面试轮次：{track.get('company') or ''} · 第{interview_store.get_round(rid)['round_number']}轮")
    return {"ok": True, "data": interview_store.get_round(rid)}


@app.get("/api/v2/interview-rounds/{rid}/workspace")
def interview_round_workspace_v2(rid: int):
    data = interview_store.round_workspace(rid)
    if not data:
        raise HTTPException(404, "面试轮次不存在")
    return {"ok": True, "data": data}


@app.put("/api/v2/interview-rounds/{rid}")
def update_interview_round_v2(rid: int, body: dict):
    item = interview_store.update_round(rid, body)
    if not item:
        raise HTTPException(404, "面试轮次不存在")
    _commit(f"更新面试轮次：第{item.get('round_number')}轮")
    return {"ok": True, "data": item}


@app.post("/api/v2/interview-rounds/{rid}/documents")
def link_round_document_v2(rid: int, body: RoundDocumentLinkIn):
    round_ = interview_store.get_round(rid)
    if not round_:
        raise HTTPException(404, "面试轮次不存在")
    source = interview_store.get_document(body.document_id)
    if not source:
        raise HTTPException(404, "文档不存在")
    if body.use_type not in {"reference", "copied", "generated", "inherited"}:
        raise HTTPException(400, "不支持的文档使用方式")
    document_id = body.document_id
    use_type = body.use_type
    if body.use_type == "copied":
        document_id = interview_store.create_document({
            "title": source["title"],
            "document_type": source["document_type"],
            "body": source.get("body") or "",
            "scope_type": "track",
            "track_id": round_["track_id"],
            "round_id": rid,
            "folder_key": source.get("folder_key"),
            "source_type": "inheritance",
            "source_ref_type": "document",
            "source_ref_id": source["id"],
            "metadata": {
                "copied_from_document_id": source["id"],
                "copied_for_round_id": rid,
            },
            "change_summary": "复制到本轮，建立独立可编辑版本",
            "created_by": "user",
        })
        use_type = "generated"
    interview_store.link_round_document(rid, document_id, use_type, body.purpose)
    _commit("复制本轮准备文档" if body.use_type == "copied" else "关联本轮准备文档")
    return {"ok": True, "items": interview_store.list_round_documents(rid)}


@app.delete("/api/v2/interview-rounds/{rid}/documents/{did}")
def remove_round_document_v2(rid: int, did: int):
    result = interview_store.remove_round_document(rid, did)
    if not result:
        raise HTTPException(404, "本轮没有这篇准备文档")
    _commit("重置本轮核心准备文档" if result["action"] == "reset" else "移除本轮准备文档")
    return {"ok": True, "data": result, "items": interview_store.list_round_documents(rid)}


@app.get("/api/v2/interview-rounds/{rid}/document-candidates")
def round_document_candidates_v2(rid: int):
    round_ = interview_store.get_round(rid)
    if not round_:
        raise HTTPException(404, "面试轮次不存在")
    return {"ok": True, "items": interview_store.list_document_candidates(round_["track_id"], rid)}


@app.post("/api/v2/interview-rounds/{rid}/inherit")
def inherit_round_context_v2(rid: int):
    """Copy prior-round knowledge into isolated, editable documents for this round."""
    round_ = interview_store.get_round(rid)
    if not round_:
        raise HTTPException(404, "面试轮次不存在")
    prior_id = round_.get("inherited_from_round_id")
    if not prior_id:
        prior = [x for x in interview_store.list_rounds(round_["track_id"])
                 if x["round_number"] < round_["round_number"]]
        prior_id = prior[-1]["id"] if prior else None
    if not prior_id:
        raise HTTPException(400, "还没有可继承的上一轮面试")
    current_links = interview_store.list_round_documents(rid)
    copied_source_ids = {x.get("source_ref_id") for x in current_links if x.get("source_ref_type") == "document"}
    inherited_titles = []
    for item in interview_store.list_round_documents(prior_id):
        if item.get("document_type") in {"round_preparation", "answer_sheet", "review_report", "next_plan"}:
            if item["document_id"] in copied_source_ids:
                continue
            document_id = interview_store.create_document({
                "title": item["title"],
                "document_type": item["document_type"],
                "body": item.get("body") or "",
                "scope_type": "track",
                "track_id": round_["track_id"],
                "round_id": rid,
                "source_type": "inheritance",
                "source_ref_type": "document",
                "source_ref_id": item["document_id"],
                "metadata": {
                    "inherited_from_round_id": prior_id,
                    "inherited_from_document_id": item["document_id"],
                },
                "change_summary": "从上一轮复制，建立本轮独立版本",
                "created_by": "system",
            })
            interview_store.link_round_document(rid, document_id, "generated", "继承自上一轮", prior_id)
            inherited_titles.append(item.get("title") or "")
    current = interview_store.ensure_round_documents(rid)
    prep_id = current.get('preparation_document_id')
    prep = interview_store.get_document(prep_id) if prep_id else None
    if prep and '## 本轮继承后的取舍' not in (prep.get('body') or ''):
        lines = ["", "## 本轮继承后的取舍", "", "### 保留", "- 从上一轮保留的有效答法 / 证据：", "", "### 修复", "- 上一轮已暴露的薄弱点：", "", "### 新增", "- 本轮新增考察重点：", "", "### 停止", "- 不再沿用的材料或回答路径：", "", "### 已复制为本轮独立版本", *[f"- {title}" for title in inherited_titles]]
        interview_store.update_document(prep_id, {"body": (prep.get('body') or '') + '\n'.join(lines), "change_summary": "建立上一轮继承取舍清单", "created_by": "system"})
    interview_store.update_round(rid, {"status": "preparing"})
    _commit("继承上一轮面试准备")
    return {"ok": True, "items": interview_store.list_round_documents(rid)}


@app.post("/api/v2/interview-rounds/{rid}/transcript-sources")
def add_transcript_source_v2(rid: int, body: TranscriptSourceV2In):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    if not body.raw_text.strip():
        raise HTTPException(400, "逐字稿内容不能为空")
    if body.source_kind not in INTERVIEW_SOURCE_KINDS:
        raise HTTPException(400, "不支持的资料类型")
    content_hash = hashlib.sha256(body.raw_text.strip().encode("utf-8")).hexdigest()
    sid = interview_store.create_transcript_source(rid, {**body.dict(), "content_hash": content_hash})
    interview_store.update_round(rid, {"status": "interviewed"})
    _commit("保存面试原始资料")
    return {"ok": True, "data": {"id": sid, "content_hash": content_hash}}


@app.get("/api/v2/interview-rounds/{rid}/transcript-sources")
def list_transcript_sources_v2(rid: int):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    return {"ok": True, "items": interview_store.list_transcript_sources(rid)}


@app.get("/api/v2/interview-rounds/{rid}/transcript-sources/{source_id}")
def get_transcript_source_v2(rid: int, source_id: int):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    source = interview_store.get_transcript_source(rid, source_id)
    if not source:
        raise HTTPException(404, "本轮资料不存在")
    return {"ok": True, "data": source}


@app.delete("/api/v2/interview-rounds/{rid}/transcript-sources/{source_id}")
def delete_transcript_source_v2(rid: int, source_id: int):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    source = interview_store.get_transcript_source(rid, source_id)
    if not source:
        raise HTTPException(404, "本轮资料不存在")
    for task in db.list_agent_tasks(
        object_type="interview_round", object_id=rid, limit=200
    ):
        if task.get("status") not in {"queued", "active"}:
            continue
        try:
            context_data = json.loads(task.get("context_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            context_data = {}
        if int(context_data.get("source_id") or 0) == source_id:
            raise HTTPException(409, "这份资料正在整理中，完成后再删除")
    answer_sheet_preserved = bool(interview_store.get_exam(rid))
    deleted, remaining_count = interview_store.delete_transcript_source(rid, source_id)
    if not deleted:
        raise HTTPException(404, "本轮资料不存在")
    if remaining_count == 0 and not answer_sheet_preserved:
        interview_store.update_round(rid, {"status": "preparing"})
    _commit("删除面试原始资料")
    return {
        "ok": True,
        "data": {
            "deleted_id": source_id,
            "remaining_count": remaining_count,
            "answer_sheet_preserved": answer_sheet_preserved,
        },
    }


@app.get("/api/v2/interview-rounds/{rid}/participants")
def list_interview_participants_v2(rid: int):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    return {"ok": True, "items": interview_store.list_participants(rid)}


@app.put("/api/v2/interview-rounds/{rid}/participants")
def save_interview_participants_v2(rid: int, bodies: list[InterviewParticipantV2In]):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    self_count = sum(1 for x in bodies if x.role == "self")
    if self_count > 1:
        raise HTTPException(400, "一场面试只能标记一位本人")
    saved = [interview_store.upsert_participant(rid, item.dict()) for item in bodies]
    _commit("确认面试参与者")
    return {"ok": True, "items": saved}


@app.post("/api/v2/review-actions/{action_id}/apply")
def apply_review_action_v2(action_id: int):
    result = interview_store.apply_review_action(action_id)
    if not result:
        raise HTTPException(404, "复盘行动不存在")
    _commit("采纳面试复盘行动到本轮准备")
    return {"ok": True, "data": result}


@app.patch("/api/v2/review-actions/{action_id}")
def update_review_action_v2(action_id: int, body: ReviewActionUpdateIn):
    item = interview_store.update_review_action(
        action_id, {key: value for key, value in body.dict().items() if value is not None}
    )
    if not item:
        raise HTTPException(404, "复盘行动不存在")
    _commit("更新面试复盘行动")
    return {"ok": True, "data": item}


@app.put("/api/v2/interview-questions/{question_id}/classification")
def save_interview_question_classification_v2(
    question_id: int, body: InterviewQuestionClassificationIn
):
    for link in body.links:
        if link.get("entity_type") not in {
            "project", "experience", "portfolio", "knowledge", "job_track", "general"
        }:
            raise HTTPException(400, "不支持的关联对象类型")
        if not str(link.get("entity_title") or "").strip():
            raise HTTPException(400, "关联对象名称不能为空")
    item = interview_store.save_question_classification(question_id, body.dict())
    if not item:
        raise HTTPException(404, "面试题目不存在")
    _commit("确认面试真题标签与经历关联")
    return {"ok": True, "data": item}


@app.get("/api/v2/interview-questions/{question_id}/detail")
def get_interview_question_v2(question_id: int):
    item = interview_store.get_question(question_id)
    if not item:
        raise HTTPException(404, "面试题目不存在")
    return {"ok": True, "data": item}


@app.put("/api/v2/interview-questions/{question_id}/answer")
def save_interview_question_answer_v2(
    question_id: int, body: InterviewQuestionAnswerIn
):
    if not body.body.strip():
        raise HTTPException(400, "答案不能为空")
    try:
        version = interview_store.save_question_answer_version(
            question_id, {
                "answer_type": "user",
                "body": body.body,
                "created_by": "user",
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not version:
        raise HTTPException(404, "面试题目不存在")
    _commit("更新面试真题答案")
    return {
        "ok": True,
        "data": {
            "version": version,
            "question": interview_store.get_question(question_id),
        },
    }


@app.put("/api/v2/interview-questions/{question_id}/text")
def save_interview_question_text_v2(
    question_id: int, body: InterviewQuestionTextIn
):
    question = body.question.strip()
    if not interview_store.update_question_text(question_id, question):
        raise HTTPException(404, "面试题目不存在")
    _commit("更新面试真题题目")
    return {"ok": True, "data": interview_store.get_question(question_id)}


def _interview_question_context(question: dict) -> str:
    """Build bounded evidence for answer coaching without inventing experience facts."""
    parts = []
    track = db.get_job_track(question.get("track_id")) if question.get("track_id") else None
    if track:
        parts.append("【目标岗位】\n" + "\n".join(
            str(value) for value in (
                f"{track.get('company') or ''} · {track.get('role') or ''}",
                (track.get("jd") or "")[:5000],
            ) if value
        ))
    link = (question.get("entity_links") or [{}])[0]
    entity_type, entity_id = link.get("entity_type"), link.get("entity_id")
    entity = None
    if entity_type in {"project", "portfolio"} and entity_id:
        entity = db.get_project(entity_id)
    elif entity_type == "experience" and entity_id:
        entity = db.get_experience(entity_id)
    if entity:
        allowed = (
            "name", "company", "role", "one_liner", "description", "document",
            "technologies", "keywords", "start_date", "end_date",
        )
        details = [
            f"{key}：{entity.get(key)}" for key in allowed if entity.get(key)
        ]
        parts.append("【关联经历或项目】\n" + "\n".join(details)[:9000])
    return "\n\n".join(parts)[:14000]


def _parse_interview_ai_answer(raw: str) -> tuple[str, str]:
    """Long answers use delimiters rather than fragile JSON escaping."""
    text = str(raw or "").strip()
    answer_marker = "===ANSWER==="
    coaching_marker = "===COACHING==="
    if answer_marker in text:
        text = text.split(answer_marker, 1)[1]
    if coaching_marker in text:
        answer, coaching = text.split(coaching_marker, 1)
    else:
        answer, coaching = text, ""
    return answer.strip(), coaching.strip()


_QUESTION_COACHING_SECTIONS = (
    "INTENT", "WHY_ASKED", "ANSWER_SUMMARY", "STRENGTHS", "ISSUES",
    "SATISFACTION_CRITERIA", "ANSWER_STRATEGY", "IMPROVED_ANSWER",
    "FOLLOWUPS", "EVIDENCE_GAPS", "CONFIDENCE_NOTE",
)


def _parse_interview_question_coaching(raw: str) -> dict:
    """Parse a long coaching document without placing prose inside JSON."""
    text = str(raw or "").strip()
    markers = "|".join(re.escape(item) for item in _QUESTION_COACHING_SECTIONS)
    matches = list(re.finditer(rf"===({markers})===", text))
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end():end].strip()

    def bullet_list(name):
        value = sections.get(name, "")
        items = []
        for line in value.splitlines():
            clean = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line).strip()
            if clean:
                items.append(clean)
        return items

    return {
        "interviewer_intent": sections.get("INTENT", ""),
        "why_asked": sections.get("WHY_ASKED", ""),
        "answer_summary": sections.get("ANSWER_SUMMARY", ""),
        "strengths": bullet_list("STRENGTHS"),
        "issues": bullet_list("ISSUES"),
        "satisfaction_criteria": bullet_list("SATISFACTION_CRITERIA"),
        "answer_strategy": sections.get("ANSWER_STRATEGY", ""),
        "improved_answer": sections.get("IMPROVED_ANSWER", ""),
        "followups": bullet_list("FOLLOWUPS"),
        "evidence_gaps": bullet_list("EVIDENCE_GAPS"),
        "confidence_note": sections.get("CONFIDENCE_NOTE", ""),
    }


def _valid_interview_question_coaching(item: dict, has_answer: bool) -> bool:
    required_text = (
        len(item.get("interviewer_intent") or "") >= 15
        and len(item.get("why_asked") or "") >= 15
        and len(item.get("answer_strategy") or "") >= 25
    )
    required_lists = (
        len(item.get("issues") or []) >= 1
        and len(item.get("satisfaction_criteria") or []) >= 2
    )
    answer_ok = (
        not has_answer
        or (
            len(item.get("answer_summary") or "") >= 12
            and len(item.get("improved_answer") or "") >= 25
        )
    )
    return required_text and required_lists and answer_ok


_QUESTION_COACH_DISCUSSION_SECTIONS = (
    "REPLY", "UPDATE_INTENT", "UPDATE_WHY_ASKED", "UPDATE_ISSUES",
    "UPDATE_CRITERIA", "UPDATE_STRATEGY", "UPDATE_ANSWER",
    "UPDATE_FOLLOWUPS", "UPDATE_EVIDENCE_GAPS",
)


def _parse_question_coach_discussion(raw: str) -> dict:
    text = str(raw or "").strip()
    markers = "|".join(
        re.escape(item) for item in _QUESTION_COACH_DISCUSSION_SECTIONS
    )
    matches = list(re.finditer(rf"===({markers})===", text))
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end():end].strip()

    def list_value(name):
        values = []
        for line in sections.get(name, "").splitlines():
            clean = re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line).strip()
            if clean and clean not in {"无", "不修改", "无需修改"}:
                values.append(clean)
        return values

    proposal = {}
    scalar_map = {
        "interviewer_intent": "UPDATE_INTENT",
        "why_asked": "UPDATE_WHY_ASKED",
        "answer_strategy": "UPDATE_STRATEGY",
        "improved_answer": "UPDATE_ANSWER",
    }
    for field, section in scalar_map.items():
        value = sections.get(section, "").strip()
        if value and value not in {"无", "不修改", "无需修改"}:
            proposal[field] = value
    list_map = {
        "issues": "UPDATE_ISSUES",
        "satisfaction_criteria": "UPDATE_CRITERIA",
        "followups": "UPDATE_FOLLOWUPS",
        "evidence_gaps": "UPDATE_EVIDENCE_GAPS",
    }
    for field, section in list_map.items():
        values = list_value(section)
        if values:
            proposal[field] = values
    return {"reply": sections.get("REPLY", "").strip(), "proposal": proposal}


def _question_coaching_document(coaching: dict | None) -> str:
    item = coaching or {}
    def lines(values):
        return "\n".join(f"- {value}" for value in (values or [])) or "无"
    return f"""面试官意图：{item.get('interviewer_intent') or '待分析'}
为什么问：{item.get('why_asked') or '待分析'}
原回答问题：
{lines(item.get('issues'))}
满意标准：
{lines(item.get('satisfaction_criteria'))}
作答路径：{item.get('answer_strategy') or '待分析'}
当前参考答法：{item.get('improved_answer') or '待生成'}
可能追问：
{lines(item.get('followups'))}
证据缺口：
{lines(item.get('evidence_gaps'))}"""


def _generate_interview_question_coaching(
    question: dict, source_answer: str, instruction: str | None
) -> tuple[dict, dict]:
    material = "\n\n".join(part for part in (
        f"【题目】\n{question.get('normalized_question') or question.get('original_question')}",
        f"【已有意图标签】\n{question.get('intent') or '待识别'}",
        f"【候选人的真实回答】\n{source_answer}" if source_answer else "【候选人的真实回答】\n未可靠识别到回答",
        _interview_question_context(question),
        f"【用户补充要求】\n{instruction}" if instruction else "",
    ) if part)
    system = """你是严谨的资深面试官与面试教练。你的任务不是直接代写答案，
而是对一道真实面试题完成可长期保存的逐题批改。

分析要求：
1. 先解释面试官真正想验证什么、为什么会沿上下文问到这里。
2. 诊断必须引用候选人原回答中的具体内容或明确指出缺失，禁止用空泛形容词。
3. 区分“表达问题”“逻辑问题”“事实或证据缺口”“岗位匹配风险”。
4. 满意标准必须说明一份好回答需要包含什么，而不是只说要更有结构。
5. 改进答案只能重组输入中已出现的事实；不得编造使用经历、指标、职责或结果。
6. 事实不足时写入 EVIDENCE_GAPS，并在改进答案中使用“[待补充：...]”。
7. 即使更换模型，输出章节和含义也必须完全一致。

严格使用以下纯文本分隔符，不要返回 JSON，不要增加其他章节：
===INTENT===
面试官真正想验证的核心判断
===WHY_ASKED===
结合题目、岗位和可能的追问链解释为什么问
===ANSWER_SUMMARY===
用完整语义概括候选人原回答，不增加事实
===STRENGTHS===
- 原回答中具体有效的部分
===ISSUES===
- 问题类型：具体证据与影响
===SATISFACTION_CRITERIA===
- 面试官满意所需的具体要素
===ANSWER_STRATEGY===
建议的作答顺序、取舍和每一步应回答什么
===IMPROVED_ANSWER===
基于真实事实重组的完整口语答案
===FOLLOWUPS===
- 面试官最可能继续追问的问题
===EVIDENCE_GAPS===
- 需要候选人补充确认的事实
===CONFIDENCE_NOTE===
哪些是逐字稿事实，哪些是基于岗位上下文的推断"""
    resolved = ai.resolve_model_profile("deep_reasoning")
    primary = resolved.get("provider") or {}
    candidates = [
        provider for provider in [primary] + list(primary.get("_fallback_providers") or [])
        if provider and provider.get("id") != "zhipu-vision"
    ]
    if not candidates:
        candidates = [None]
    failures = []
    for candidate in candidates:
        try:
            kwargs = {
                "system": system, "max_tokens": 3600, "timeout": 180,
            }
            if candidate:
                kwargs["provider"] = {**candidate, "_fallback_providers": []}
            raw = ai.chat([{"role": "user", "content": material}], **kwargs)
            parsed = _parse_interview_question_coaching(raw)
            if _valid_interview_question_coaching(parsed, bool(source_answer.strip())):
                call_info = ai.get_last_call_info() or {}
                return parsed, {
                    "model_provider": (
                        call_info.get("provider_name")
                        or call_info.get("provider_id")
                        or (candidate or {}).get("name")
                        or (candidate or {}).get("id")
                    ),
                    "model_name": call_info.get("model") or (candidate or {}).get("model"),
                }
            failures.append("输出缺少必要批改章节")
        except Exception as exc:
            failures.append(str(exc))
    raise RuntimeError(failures[-1] if failures else "没有可用模型")


@app.post("/api/v2/interview-questions/{question_id}/ai-answer")
def generate_interview_question_ai_answer_v2(
    question_id: int, body: InterviewQuestionAIAnswerIn
):
    question = interview_store.get_question(question_id)
    if not question:
        raise HTTPException(404, "面试题目不存在")
    workspace = question.get("answer_workspace") or {}
    source_answer = (
        workspace.get("current_answer")
        or workspace.get("evidence_answer")
        or ""
    )
    transcript = ""
    if body.mode == "recover":
        sources = interview_store.list_transcript_sources(question["round_id"])
        transcript = "\n\n".join(
            f"【资料 {index + 1}】\n{item.get('raw_text') or ''}"
            for index, item in enumerate(sources)
        )[:24000]
    mode_instruction = {
        "recover": (
            "从逐字稿中找出王玺对该题的真实回答，去掉口头语和重复，"
            "只重组原意，不补充逐字稿中不存在的事实。若无法可靠识别，"
            "ANSWER 区只写“未能从逐字稿可靠识别”，并在 COACHING 说明原因。"
        ),
        "improve": (
            "在不虚构事实的前提下优化现有回答，使其更直接、有结构、"
            "有证据，并符合目标岗位。"
        ),
        "generate": (
            "基于已知岗位和经历生成一份可编辑的参考答案。事实不足时"
            "明确使用待补充占位符，不得编造数字、职责或结果。"
        ),
    }[body.mode]
    material = "\n\n".join(part for part in (
        f"【题目】\n{question.get('normalized_question') or question.get('original_question')}",
        f"【面试官意图】\n{question.get('intent') or '待识别'}",
        f"【当前答案】\n{source_answer}" if source_answer else "",
        _interview_question_context(question),
        f"【本轮逐字稿】\n{transcript}" if transcript else "",
        f"【用户补充要求】\n{body.instruction}" if body.instruction else "",
    ) if part)
    system = """你是严谨的面试教练。必须区分真实回答、可验证事实和建议，
不得把推断写成候选人做过的事实。请严格使用以下纯文本分隔符返回：
===ANSWER===
一份可直接继续编辑的完整答案
===COACHING===
用简洁条目说明回答逻辑、仍缺的证据、可能追问。不要返回 JSON。"""
    try:
        raw = ai.chat(
            [{"role": "user", "content": mode_instruction + "\n\n" + material}],
            system=system, max_tokens=2200, profile_key="writing",
        )
    except Exception as exc:
        raise HTTPException(502, f"AI 辅助失败：{exc}") from exc
    answer, coaching = _parse_interview_ai_answer(raw)
    if not answer:
        raise HTTPException(502, "AI 没有返回可用答案")
    call_info = ai.get_last_call_info() or {}
    version = interview_store.save_question_answer_version(
        question_id, {
            "answer_type": "ai",
            "body": answer,
            "coaching": coaching,
            "instruction": body.instruction,
            "source_answer_id": workspace.get("evidence_answer_id"),
            "model_provider": call_info.get("provider_name") or call_info.get("provider_id"),
            "model_name": call_info.get("model"),
            "created_by": "ai",
        },
    )
    _commit("生成面试真题 AI 辅助答案")
    return {
        "ok": True,
        "data": {
            "version": version,
            "answer": answer,
            "coaching": coaching,
            "question": interview_store.get_question(question_id),
        },
    }


@app.post("/api/v2/interview-questions/{question_id}/coach")
def coach_interview_question_v2(
    question_id: int, body: InterviewQuestionCoachIn
):
    question = interview_store.get_question(question_id)
    if not question:
        raise HTTPException(404, "面试题目不存在")
    workspace = question.get("answer_workspace") or {}
    source_answer = str(
        workspace.get("current_answer")
        or workspace.get("evidence_answer")
        or ""
    ).strip()
    try:
        coaching, model = _generate_interview_question_coaching(
            question, source_answer, body.instruction
        )
    except Exception as exc:
        raise HTTPException(502, f"深度批改失败：{exc}") from exc
    version = interview_store.save_question_coaching_version(question_id, {
        **coaching,
        "source_answer_version": workspace.get("current_version") or 0,
        "instruction": body.instruction,
        **model,
    })
    _commit("生成面试真题深度批改")
    return {
        "ok": True,
        "data": {
            "version": version,
            "coaching": coaching,
            "question": interview_store.get_question(question_id),
        },
    }


@app.post("/api/v2/interview-questions/{question_id}/coach/tasks")
def start_interview_question_coach_task_v2(
    question_id: int, body: InterviewQuestionCoachIn
):
    question = interview_store.get_question(question_id)
    if not question:
        raise HTTPException(404, "面试题目不存在")
    round_id = int(question["round_id"])
    for item in db.list_agent_tasks(
        object_type="interview_round", object_id=round_id, limit=50
    ):
        if (
            item.get("task_type") != "interview_question_coach"
            or item.get("status") not in {"queued", "active"}
        ):
            continue
        try:
            context_data = json.loads(item.get("context_json") or "{}")
        except json.JSONDecodeError:
            continue
        if int(context_data.get("question_id") or 0) == question_id:
            task = db.get_agent_task(item["id"]) or item
            return {
                "ok": True,
                "data": _interview_task_progress(task),
                "reused": True,
            }

    task_id = db.create_agent_task({
        "task_type": "interview_question_coach",
        "title": f"深度批改：{(question.get('normalized_question') or question.get('original_question') or '')[:60]}",
        "instruction": "还原面试官意图、诊断真实回答并形成可编辑批改版本",
        "object_type": "interview_round",
        "object_id": round_id,
        "track_id": question.get("track_id"),
        "status": "queued",
        "assigned_expert": "review_analyst",
        "context_json": json.dumps({
            "question_id": question_id,
            "instruction": body.instruction,
        }, ensure_ascii=False),
    })
    db.create_agent_event({
        "task_id": task_id,
        "event_type": "queued",
        "label": "深度批改已进入后台队列",
        "detail": "可以离开当前页面，完成后 Caddie 会通知你",
        "status": "pending",
    })
    _schedule_interview_question_coach(task_id, question_id, body.instruction)
    _commit("开始面试真题深度批改")
    return {
        "ok": True,
        "data": _interview_task_progress(db.get_agent_task(task_id)),
        "reused": False,
    }


@app.post("/api/v2/interview-questions/{question_id}/coach/messages")
def discuss_interview_question_coaching_v2(
    question_id: int, body: InterviewQuestionCoachMessageIn
):
    question = interview_store.get_question(question_id)
    if not question:
        raise HTTPException(404, "面试题目不存在")
    user_message = str(body.message or "").strip()
    if not user_message:
        raise HTTPException(400, "请输入你不同意、想追问或想修改的内容")
    workspace = question.get("answer_workspace") or {}
    coaching = workspace.get("question_coaching")
    if not coaching:
        raise HTTPException(409, "请先完成一次深度批改")
    interview_store.save_question_coaching_message(question_id, {
        "role": "user", "content": user_message,
        "coaching_version": coaching.get("version") or 0,
        "answer_version": workspace.get("current_version") or 0,
    })
    history = (workspace.get("coaching_messages") or [])[-8:]
    history_text = "\n".join(
        f"{'用户' if item.get('role') == 'user' else '教练'}：{item.get('content')}"
        for item in history
    )
    material = "\n\n".join((
        f"【题目】\n{question.get('normalized_question') or question.get('original_question')}",
        _interview_question_context(question),
        f"【候选人当前答案】\n{workspace.get('current_answer') or '暂无'}",
        f"【当前批改文档】\n{_question_coaching_document(coaching)}",
        f"【此前对话】\n{history_text}" if history_text else "",
        f"【用户本轮消息】\n{user_message}",
    ))
    system = """你是与候选人共同打磨真实面试答案的资深面试教练。
这不是一次重新生成报告的任务，而是一轮有上下文的协商。

工作原则：
1. 先直接回应用户的异议或追问。用户的事实纠正优先级最高，但不要盲目迎合；
   如果不同意，要解释判断依据，并指出还需要什么证据。
2. 必须区分“对事实的纠正”“对面试官意图的不同理解”“表达偏好”和
   “答案策略调整”。
3. 只有确实需要修改现有批改文档时，才填写对应 UPDATE 章节。
4. 不得编造候选人的经历、数字、职责或结果。事实不足时只能列为证据缺口。
5. UPDATE_ANSWER 必须是可口述的完整答案；若本轮讨论尚不足以更新，写“无”。

严格使用以下分隔符返回，不要返回 JSON：
===REPLY===
直接回应用户，解释你的判断、依据和建议
===UPDATE_INTENT===
新的面试官意图；无需修改写“无”
===UPDATE_WHY_ASKED===
新的提问链解释；无需修改写“无”
===UPDATE_ISSUES===
- 更新后的问题清单；无需修改写“无”
===UPDATE_CRITERIA===
- 更新后的满意标准；无需修改写“无”
===UPDATE_STRATEGY===
更新后的作答路径；无需修改写“无”
===UPDATE_ANSWER===
更新后的完整参考答法；无需修改写“无”
===UPDATE_FOLLOWUPS===
- 更新后的可能追问；无需修改写“无”
===UPDATE_EVIDENCE_GAPS===
- 更新后的事实缺口；无需修改写“无”"""
    try:
        raw = ai.chat(
            [{"role": "user", "content": material}],
            system=system, max_tokens=2800, profile_key="deep_reasoning",
        )
    except Exception as exc:
        raise HTTPException(502, f"教练对话失败：{exc}") from exc
    parsed = _parse_question_coach_discussion(raw)
    if len(parsed.get("reply") or "") < 10:
        raise HTTPException(502, "教练没有返回可用的解释")
    call_info = ai.get_last_call_info() or {}
    assistant_message = interview_store.save_question_coaching_message(
        question_id, {
            "role": "assistant",
            "content": parsed["reply"],
            "proposal": parsed["proposal"],
            "coaching_version": coaching.get("version") or 0,
            "answer_version": workspace.get("current_version") or 0,
            "model_provider": (
                call_info.get("provider_name") or call_info.get("provider_id")
            ),
            "model_name": call_info.get("model"),
        },
    )
    _commit("记录面试真题教练对话")
    return {
        "ok": True,
        "data": {
            "message": assistant_message,
            "question": interview_store.get_question(question_id),
        },
    }


@app.post("/api/v2/interview-questions/{question_id}/coach/messages/{message_id}/apply")
def apply_interview_question_coaching_message_v2(
    question_id: int, message_id: int
):
    question = interview_store.get_question(question_id)
    message = interview_store.get_question_coaching_message(message_id)
    if not question or not message or message.get("question_id") != question_id:
        raise HTTPException(404, "候选修改不存在")
    if message.get("role") != "assistant":
        raise HTTPException(400, "该消息不包含 AI 候选修改")
    proposal = message.get("proposal") or {}
    if not proposal:
        raise HTTPException(400, "本轮对话没有建议修改批改文档")
    workspace = question.get("answer_workspace") or {}
    current = workspace.get("question_coaching") or {}
    merged = {
        key: current.get(key) for key in (
            "interviewer_intent", "why_asked", "answer_summary", "strengths",
            "issues", "satisfaction_criteria", "answer_strategy",
            "improved_answer", "followups", "evidence_gaps", "confidence_note",
        )
    }
    merged.update(proposal)
    version = interview_store.save_question_coaching_version(question_id, {
        **merged,
        "source_answer_version": workspace.get("current_version") or 0,
        "instruction": f"采纳教练对话消息 #{message_id}",
        "model_provider": message.get("model_provider"),
        "model_name": message.get("model_name"),
    })
    interview_store.mark_question_coaching_message_applied(message_id)
    _commit("采纳面试真题教练建议")
    return {
        "ok": True,
        "data": {
            "version": version,
            "question": interview_store.get_question(question_id),
        },
    }


def _parse_interview_knowledge_draft(raw: str) -> dict:
    text = str(raw or "").strip()
    marker = "===DOCUMENT==="
    if marker not in text:
        raise ValueError("模型没有返回完整知识文档")
    header, content = text.split(marker, 1)
    values = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().upper() in {"TITLE", "TOPIC"}:
            values[key.strip().upper()] = value.strip()
    content = content.strip()
    if not content:
        raise ValueError("模型返回的知识文档为空")
    title = values.get("TITLE") or "面试问题延伸知识"
    topic = values.get("TOPIC") or title
    return {"title": title[:240], "topic": topic[:120], "content": content}


@app.post("/api/v2/interview-questions/{question_id}/knowledge-draft")
def draft_interview_question_knowledge_v2(
    question_id: int, body: InterviewQuestionKnowledgeDraftIn
):
    question = interview_store.get_question(question_id)
    if not question:
        raise HTTPException(404, "面试题目不存在")
    workspace = question.get("answer_workspace") or {}
    coaching = workspace.get("question_coaching") or {}
    messages = workspace.get("coaching_messages") or []
    discussion = "\n".join(
        f"{'用户' if item.get('role') == 'user' else '面试教练'}："
        f"{item.get('content') or ''}"
        for item in messages[-10:]
    )
    material = "\n\n".join(filter(None, (
        f"【面试题】\n{question.get('normalized_question') or question.get('original_question') or ''}",
        f"【候选人的真实回答】\n{workspace.get('evidence_answer') or '暂无可靠回答'}",
        f"【当前可编辑答案】\n{workspace.get('current_answer') or '暂无'}",
        f"【逐题批改结论】\n{_question_coaching_document(coaching)}" if coaching else "",
        f"【教练讨论】\n{discussion}" if discussion else "",
        f"【用户补充要求】\n{body.instruction.strip()}" if body.instruction else "",
    )))
    system = """你是 Caddie 的知识沉淀编辑。请从一道真实面试题及其讨论中，
提炼一个值得长期复用的知识主题，并形成可继续学习和编辑的结构化文档。

要求：
1. 不要把文档写成面试复盘，也不要只给一段参考答案；应围绕核心主题补足概念、
   判断框架、应用边界、具体例子和可继续研究的问题。
2. 严格区分候选人的事实、面试教练的判断和通用知识。不得编造经历或数据。
3. 标题应是知识主题，例如“数字员工的价值、边界与激励机制”，不要写“某题复盘”。
4. 正文用清晰的小节组织，适合在所见即所得编辑器中继续修改。
5. 大段正文不得放进 JSON。严格按下面格式返回：

TITLE: 知识文档标题
TOPIC: 简短主题标签
===DOCUMENT===
# 标题

## 核心问题
...
"""
    try:
        raw = ai.chat(
            [{"role": "user", "content": material}],
            system=system, max_tokens=3200, profile_key="deep_reasoning",
        )
        draft = _parse_interview_knowledge_draft(raw)
    except Exception as exc:
        raise HTTPException(502, f"知识提炼失败：{exc}") from exc
    return {
        "ok": True,
        "data": {
            **draft,
            "track_id": question.get("track_id"),
            "scope_type": "track" if question.get("track_id") else "global",
        },
    }


@app.post("/api/v2/interview-questions/{question_id}/knowledge")
def save_interview_question_knowledge_v2(
    question_id: int, body: InterviewQuestionKnowledgeSaveIn
):
    question = interview_store.get_question(question_id)
    if not question:
        raise HTTPException(404, "面试题目不存在")
    track_id = question.get("track_id") if body.scope_type == "track" else None
    folder_id = None
    if body.scope_type == "track":
        if not track_id or not db.get_job_track(track_id):
            raise HTTPException(400, "当前真题没有关联可用的求职岗位")
        folders, _ = db.ensure_track_knowledge_folders(track_id)
        folder = next((item for item in folders if item.get("name") == "面试准备"), None)
        if not folder:
            raise HTTPException(500, "岗位准备目录不完整")
        folder_id = folder["id"]
    data = {
        "title": body.title.strip(),
        "content": body.content.strip(),
        "scope_type": body.scope_type,
        "track_id": track_id,
        "topic": (body.topic or "").strip() or None,
        "mastery": "learning",
        "folder_id": folder_id,
        "status": "active",
        "source_type": "interview_question",
        "source_ref_id": question_id,
    }
    kid = db.create_knowledge_item(data)
    _workspace_event(
        "knowledge_created", "knowledge_item", kid,
        f"从真题沉淀知识：{body.title.strip()}",
        body.scope_type, track_id,
    )
    _commit(f"从面试真题沉淀知识：{body.title.strip()}")
    return {"ok": True, "item": _enrich_knowledge_item(db.get_knowledge_item(kid))}


@app.get("/api/v2/interview-questions/classification-options")
def interview_question_classification_options_v2():
    experiences = db.get_experiences()
    projects = db.list_projects()
    entities = []
    for experience in experiences:
        entities.append({
            "entity_type": "experience", "entity_id": experience["id"],
            "entity_title": " · ".join(
                x for x in (experience.get("company"), experience.get("role")) if x
            ),
        })
    for project in projects:
        entities.append({
            "entity_type": "portfolio" if project.get("kind") == "personal" else "project",
            "entity_id": project["id"], "entity_title": project.get("name") or "未命名项目",
            "subtitle": " · ".join(
                x for x in (
                    project.get("experience_company"), project.get("experience_role")
                ) if x
            ),
        })
    return {
        "ok": True,
        "data": {
            "question_types": [
                "自我介绍", "基础信息", "项目深挖", "业务理解", "AI认知", "专业知识",
                "行为面试", "求职动机", "职业规划", "反问", "其他",
            ],
            "ability_keys": [
                "表达结构", "事实与证据", "问题拆解", "业务判断", "专业能力",
                "AI产品判断", "沟通协作", "执行推进", "学习能力",
                "动机稳定性", "岗位匹配",
            ],
            "tag_types": ["主题", "追问角度", "岗位能力", "知识领域"],
            "knowledge_topics": [
                "AI与Agent", "AI工具", "产品与用户", "数据分析",
                "项目协同", "金融与投行", "电商业务",
            ],
            "entities": entities,
        },
    }


def _suggest_interview_question_classifications(questions: list[dict]) -> None:
    """Seed editable classifications without pretending rule matches are confirmed facts."""
    experiences = db.get_experiences()
    projects = db.list_projects()
    entities = []
    for experience in experiences:
        title = " · ".join(
            value for value in (experience.get("company"), experience.get("role")) if value
        )
        entities.append({
            "entity_type": "experience", "entity_id": experience["id"],
            "entity_title": title, "search_text": title,
        })
    for project in projects:
        title = project.get("name") or "未命名项目"
        subtitle = " · ".join(
            value for value in (
                project.get("experience_company"), project.get("experience_role")
            ) if value
        )
        entities.append({
            "entity_type": "portfolio" if project.get("kind") == "personal" else "project",
            "entity_id": project["id"], "entity_title": title,
            # Classification needs conservative evidence. Long descriptions contain
            # generic words such as AI, analysis and product that create false links.
            "search_text": " ".join(value for value in (title, subtitle) if value),
        })

    aspect_rules = (
        (("为什么选", "为何选择", "选题", "动机"), "选题与动机"),
        (("结论", "发现什么", "结果是什么", "得出什么"), "研究结论"),
        (("数据", "样本", "变量", "模型", "方法", "怎么研究"), "研究方法与数据"),
        (("困难", "卡点", "阻力", "不配合"), "卡点与决策"),
        (("你做了什么", "你的工作", "具体负责", "如何推进"), "本人动作"),
        (("效果", "成果", "提升", "指标"), "结果与影响"),
        (("复盘", "重来", "重新做", "改进"), "复盘与改进"),
        (("怎么理解", "是什么", "区别", "看法"), "概念与业务理解"),
        (("职业", "未来", "方向", "规划"), "职业选择"),
    )
    type_rules = (
        (("自我介绍", "介绍一下自己"), "自我介绍", "表达结构"),
        (("为什么投", "为什么选择", "求职动机", "为什么来"), "求职动机", "动机稳定性"),
        (("职业规划", "未来方向", "首选方向"), "职业规划", "动机稳定性"),
        (("项目", "论文", "经历", "具体负责", "你做了什么"), "项目深挖", "事实与证据"),
        (("业务怎么理解", "岗位怎么理解", "行业怎么看"), "业务理解", "业务判断"),
        (("冲突", "协作", "不配合", "沟通"), "行为面试", "沟通协作"),
        (("优先级", "推进", "落地"), "行为面试", "执行推进"),
    )
    thesis_candidates = [
        entity for entity in entities
        if any(token in entity["search_text"].lower() for token in ("论文", "ipo", "审核问询", "定价风险"))
    ]

    for question in questions:
        text = " ".join(
            str(question.get(key) or "") for key in
            ("normalized_question", "original_question", "intent")
        )
        lowered = text.lower()
        question_type = question.get("question_type") or "其他"
        ability_key = question.get("ability_key")
        block_entity_link = False
        if any(term in text for term in (
            "能实习多长", "实习多久", "每周几天", "全时段", "硕士在读",
            "现在在哪家实习", "工作地", "到岗", "毕业时间",
        )):
            question_type, ability_key, block_entity_link = "基础信息", "岗位匹配", True
        elif any(term in text for term in ("争取留用", "留用机会", "长期投入")):
            question_type, ability_key, block_entity_link = "求职动机", "动机稳定性", True
        elif interview_store.is_ai_concept_question(text):
            question_type, ability_key = "AI认知", "AI产品判断"
        else:
            for terms, suggested_type, suggested_ability in type_rules:
                if any(term in text for term in terms):
                    question_type, ability_key = suggested_type, suggested_ability
                    break
        aspect = next(
            (label for terms, label in aspect_rules if any(term in text for term in terms)),
            "事实、方法与边界" if question_type == "项目深挖" else "岗位相关判断",
        )

        matched = None
        if not block_entity_link and "论文" in text and thesis_candidates:
            matched = thesis_candidates[0]
        if not matched and not block_entity_link:
            best_score = 0
            stopwords = {
                "项目", "实习", "实习生", "产品", "分析", "研究", "系统",
                "设计", "工作", "实践", "个人", "求职", "管家", "相关",
                "能力", "业务", "数据", "自动化", "模型",
            }
            for entity in entities:
                haystack = entity["search_text"].lower()
                tokens = [
                    token for token in re.split(r"[\s·|/（）()，,：:；;_-]+", haystack)
                    if len(token) >= 2 and token not in stopwords
                ]
                score = sum(
                    4 if len(token) >= 4 and token in lowered
                    else 2 if len(token) >= 2 and token in lowered
                    else 0
                    for token in tokens
                )
                if score > best_score:
                    matched, best_score = entity, score
            # Generic interview questions stay unlinked. A suggestion needs either
            # one distinctive phrase or multiple shorter identity tokens.
            if best_score < 4:
                matched = None

        tags = [
            {
                "type": "追问角度", "value": aspect, "confidence": 0.72,
                "source": "rule", "confirmed": False,
            }
        ]
        for topic in interview_store.infer_question_topics(text):
            tags.append({
                "type": "知识领域", "value": topic, "confidence": 0.9,
                "source": "system", "confirmed": False,
            })
        if matched:
            tags.append({
                "type": "主题", "value": matched["entity_title"], "confidence": 0.76,
                "source": "rule", "confirmed": False,
            })
        links = []
        if matched:
            links.append({
                "entity_type": matched["entity_type"],
                "entity_id": matched["entity_id"],
                "entity_title": matched["entity_title"],
                "relation": "asked_about",
                "aspect": aspect,
                "confidence": 0.76,
                "source": "rule",
                "confirmed": False,
            })
        interview_store.save_question_classification(question["id"], {
            "question_type": question_type,
            "ability_key": ability_key,
            "tags": tags,
            "links": links,
            "confirmed": False,
        })


@app.get("/api/v2/interview-questions/linked")
def linked_interview_questions_v2(
    entity_type: str, entity_id: Optional[int] = None,
    confirmed_only: bool = True, limit: int = 100,
):
    items = interview_store.list_linked_questions(
        entity_type, entity_id, confirmed_only, limit
    )
    detailed = []
    for item in items:
        detail = interview_store.get_question(item["id"]) or item
        for key in ("entity_type", "entity_id", "entity_title", "aspect",
                    "link_confirmed", "company", "role", "round_number", "round_name"):
            if item.get(key) is not None:
                detail[key] = item[key]
        detailed.append(detail)
    return {
        "ok": True,
        "items": detailed,
    }


@app.post("/api/v2/interview-rounds/{rid}/questions/{question_id}/use-in-preparation")
def use_interview_question_in_preparation_v2(rid: int, question_id: int):
    round_ = interview_store.get_round(rid)
    question = interview_store.get_question(question_id)
    if not round_:
        raise HTTPException(404, "面试轮次不存在")
    if not question:
        raise HTTPException(404, "面试题目不存在")
    interview_store.ensure_round_documents(rid)
    round_ = interview_store.get_round(rid)
    document_id = round_.get("preparation_document_id")
    document = interview_store.get_document(document_id) if document_id else None
    if not document:
        raise HTTPException(500, "本轮准备文档不存在")
    marker = f"<!-- interview-question:{question_id} -->"
    body = document.get("body") or ""
    if marker not in body:
        link = (question.get("entity_links") or [{}])[0]
        source = " · ".join(
            value for value in (
                question.get("company"), question.get("role"),
                f"第 {question.get('round_number')} 轮" if question.get("round_number") else None,
            ) if value
        )
        addition = "\n".join((
            "", marker, "",
            f"## 历史真题：{question.get('normalized_question') or question.get('original_question')}",
            "",
            f"- 来源：{source or '历史面试'}",
            f"- 关联：{link.get('entity_title') or '待确认'}",
            f"- 追问角度：{link.get('aspect') or question.get('ability_key') or '待确认'}",
            "",
            "### 本轮答题要点",
            "",
            "- ",
            "",
        ))
        interview_store.update_document(document_id, {
            "body": body.rstrip() + addition,
            "change_summary": "引用历史面试真题到本轮准备",
            "created_by": "user",
        })
    _commit("引用历史面试真题到本轮准备")
    return {"ok": True, "data": {"document_id": document_id, "question_id": question_id}}


def _interview_task_progress(task: dict) -> dict:
    events = task.get("events") or []
    last = events[-1] if events else {}
    event_progress = {
        "queued": 8, "recovered": 12, "running": 24,
        "draft_ready": 36, "model_running": 48,
        "review_started": 24, "review_questions_ready": 38,
        "review_model_running": 48, "model_quality": 66,
        "review_document_saved": 82, "review_actions_ready": 92,
        "growth_scope_ready": 22, "growth_profile": 40,
        "growth_trend": 56, "growth_training": 72,
        "growth_documents_saved": 92,
        "ai_fallback": 66, "questions_created": 86,
        "question_context": 18, "question_intent": 38,
        "question_diagnosis": 72, "question_saved": 92,
        "completed": 100, "failed": 100,
    }
    progress = 5
    for event in events:
        progress = max(progress, event_progress.get(event.get("event_type"), progress))
    if task.get("status") in {"completed", "review", "failed", "cancelled"}:
        progress = 100
    return {
        **_agent_task_payload(task),
        "progress": progress,
        "current_label": last.get("label") or task.get("result_summary") or "等待开始",
        "current_detail": last.get("detail"),
        "is_active": task.get("status") in {"queued", "active"},
    }


@app.get("/api/v2/interview-rounds/{rid}/tasks")
def interview_round_tasks_v2(rid: int, limit: int = 10):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    tasks = db.list_agent_tasks(
        object_type="interview_round", object_id=rid, limit=limit
    )
    return {
        "ok": True,
        "items": [
            _interview_task_progress(db.get_agent_task(task["id"]) or task)
            for task in tasks
        ],
    }


def _answer_stage(question: dict) -> tuple[str, str]:
    """Map a question to a readable interview stage without inventing facts."""
    try:
        tags = json.loads(question.get("tags") or "{}")
    except Exception:
        tags = {}
    model_stage = (tags.get("stage") if isinstance(tags, dict) else "") or ""
    if model_stage and model_stage != "待确认":
        return model_stage, question.get("intent") or "结合连续追问判断本阶段的验证重点"
    text = " ".join(str(question.get(key) or "") for key in (
        "question_type", "ability_key", "intent", "normalized_question", "original_question"
    )).lower()
    mappings = (
        (("项目", "经历", "预测", "案例", "项目深挖"), ("项目与真实能力验证", "通过具体项目核验问题拆解、信息来源、本人动作和结果边界")),
        (("自我介绍", "学制", "到岗", "稳定", "时间", "背景"), ("开场与投入条件", "确认候选人的基础背景、到岗条件与投入稳定性")),
        (("业务", "专业", "行业", "产品", "岗位理解"), ("业务理解与岗位认知", "判断候选人能否理解岗位所处业务，以及关键协作和决策逻辑")),
        (("动机", "职业", "求职", "匹配", "方向"), ("动机与岗位匹配", "确认候选人的选择逻辑、长期意愿和岗位匹配度")),
        (("行为", "冲突", "分歧", "协作", "沟通", "压力"), ("工作方式与协作边界", "核验面对分歧、依赖和压力时的处理方式")),
        (("反问",), ("候选人反问", "观察候选人关注的工作范围、成长路径和真实判断标准")),
    )
    for needles, stage in mappings:
        if any(needle in text for needle in needles):
            return stage
    return "其他待确认议题", "当前材料不足以可靠判断这组问题的完整考察目的"


def _clean_spoken_answer_for_document(value: str) -> str:
    """Keep a locally generated answer readable without pretending to rewrite it.

    The AI pass is responsible for semantic reconstruction. This fallback only
    removes obvious ASR filler and whitespace so the editable document remains
    usable when no model result is available.
    """
    text = (value or "").strip()
    if not text:
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?:^|(?<=[。！？；]))\s*(?:嗯+|呃+|额+|啊+|好的|好|对的?|那个|这个)\s*[，、。！？；]*\s*", "", text)
    text = re.sub(r"(?:(?:嗯|呃|额)[，、\s]*){1,}", "", text)
    text = re.sub(r"([，。！？；])\1+", r"\1", text)
    return text.strip()


def _document_intent(value: str) -> str:
    intent = (value or "").strip()
    if not intent or "连续追问中归并" in intent or "可在答卷中继续拆分" in intent:
        return "待从原始逐字稿确认"
    return intent


def _build_answer_sheet_markdown(round_: dict, questions: list[dict], parser_note: str,
                                 bundle: dict | None = None) -> str:
    """Build the editable answer-sheet document from verified question data.

    The document is intentionally whole-interview first. Individual questions
    remain below it, but do not define the user's first reading experience.
    """
    mode_names = {"one_to_one": "单人面试", "panel": "多面试官", "group": "群面 / 多候选人", "unknown": "形式待确认"}
    mode = mode_names.get(round_.get("interview_mode"), round_.get("interview_mode") or "形式待确认")
    stages = []
    for question in questions:
        stage_name, stage_goal = _answer_stage(question)
        if stages and stages[-1]["name"] == stage_name:
            stages[-1]["questions"].append(question)
        else:
            stages.append({"name": stage_name, "goal": stage_goal, "questions": [question]})
    stage_path = "、".join(stage["name"] for stage in stages) if stages else "待整理"
    overview = (bundle or {}).get("overview") or {}
    role_insights = (bundle or {}).get("role_insights") or []
    lines = [
        f"# 第 {round_.get('round_number')} 轮面试答卷",
        "",
        "## 一、整场面试概览",
        "",
        f"- **面试形式**：{mode}",
        f"- **已重建主问题**：{len(questions)} 个",
        f"- **整理方式**：{parser_note}",
        "- **答卷边界**：已删除语气词、重复和明显断句错误；不补充现场未说过的经历、数据或结论。",
        "",
        overview.get("summary") or "本答卷先还原整场面试如何推进，再保留逐题的真实回答、追问和现场信号。优化表达请在“同步复盘”查看，不会混入真实答卷。",
        "",
        "## 二、面试推进节奏",
        "",
    ]
    if stages:
        for index, stage in enumerate(stages, 1):
            titles = "；".join((item.get("normalized_question") or item.get("original_question") or "待确认问题") for item in stage["questions"])
            intents = list(dict.fromkeys(
                intent for intent in ((item.get("intent") or "").strip() for item in stage["questions"])
                if intent and "连续追问中归并" not in intent and "可在答卷中继续拆分" not in intent
            ))
            lines += [
                f"### 阶段 {index}：{stage['name']}",
                f"- **涉及问题**：{titles}",
                f"- **验证重点**：{stage['goal']}",
                f"- **本轮证据**：{'；'.join(intents) if intents else '当前题库尚未记录明确意图，需结合原始证据补充确认'}",
                "",
            ]
    else:
        lines += ["当前尚未识别到可确认的主问题。请先确认说话人或使用 AI 整理完整答卷。", ""]
    judgment_path = overview.get("judgment_path") or stage_path
    lines += [
        "## 三、面试官的考察路径",
        "",
        f"从当前问题顺序看，本轮面试依次经过：{judgment_path}。这是一份基于问题顺序和连续追问形成的结构判断，不代表面试结果预测。",
        "",
        "下一轮准备时，应优先补充每个阶段中被连续追问的事实、判断依据和责任边界，而不是单独背诵问题清单。",
        "",
    ]
    if role_insights:
        lines += ["## 四、面试官提供的岗位信息", ""]
        for insight in role_insights:
            lines += [f"### {insight.get('title') or '岗位认知'}", insight.get("content") or "", ""]
        question_section = "五"
    else:
        question_section = "四"
    lines += [
        f"## {question_section}、逐题答卷",
        "",
    ]
    for index, question in enumerate(questions, 1):
        title = question.get("normalized_question") or question.get("original_question") or "待确认问题"
        lines += [f"## {index}. {title}", ""]
        if question.get("question_type") or question.get("ability_key"):
            lines += [f"**考察维度**：{question.get('question_type') or '待确认'} / {question.get('ability_key') or '待确认'}", ""]
        lines += [f"**面试官意图**：{_document_intent(question.get('intent'))}", ""]
        self_answer = next((answer for answer in question.get("answers") or [] if answer.get("is_self")), None)
        if self_answer:
            answer_text = self_answer.get("organized_answer") or self_answer.get("original_answer") or ""
            lines += ["### 我的回答", _clean_spoken_answer_for_document(answer_text) or "待补充", ""]
            if self_answer.get("interviewer_signal"):
                lines += ["### 连续追问与现场信号", self_answer.get("interviewer_signal"), ""]
        else:
            lines += ["### 我的回答", "当前未能可靠归属本人回答。请先在原始资料中确认说话人后再编辑。", ""]
        others = [answer for answer in question.get("answers") or [] if not answer.get("is_self")]
        if others:
            lines += ["### 其他候选人或待确认回答"]
            for answer in others:
                lines += [answer.get("organized_answer") or answer.get("original_answer") or "待确认"]
            lines += [""]
    return "\n".join(lines)


@app.post("/api/v2/interview-rounds/{rid}/answer-sheet/rebuild")
def rebuild_answer_sheet_v2(rid: int):
    """Upgrade an existing round to the whole-interview document structure."""
    round_ = interview_store.ensure_round_documents(rid)
    if not round_:
        raise HTTPException(404, "面试轮次不存在")
    questions = interview_store.get_exam(rid)
    if not questions:
        raise HTTPException(400, "还没有可整理的结构化题目")
    document_id = round_.get("answer_sheet_document_id")
    if not document_id:
        raise HTTPException(409, "答卷文档未创建")
    document, error = interview_store.update_document(document_id, {
        "title": f"第 {round_.get('round_number')} 轮面试答卷",
        "body": _build_answer_sheet_markdown(round_, questions, "基于当前已确认题目刷新"),
        "change_summary": "重建整场节奏与逐题答卷结构",
        "created_by": "system",
    })
    if error:
        raise HTTPException(409, "答卷文档当前不可更新")
    _commit("重建面试答卷结构")
    return {"ok": True, "data": document}


def _validated_exam_from_models(raw_text: str, segments: list[dict], participants: list[dict],
                                job_context: str) -> tuple[dict | None, list[dict]]:
    """Try configured providers against one stable contract.

    A provider is accepted for semantic quality, not merely because it returned
    parseable text. This keeps the product usable when users switch models.
    """
    resolved = ai.resolve_model_profile("deep_reasoning")
    primary = resolved.get("provider") or {}
    candidates = [
        provider for provider in [primary] + list(primary.get("_fallback_providers") or [])
        if provider and provider.get("id") != "zhipu-vision"
    ][:2]
    attempts = []
    best = None
    rejected_bundles = []
    prompt = interview_decompose.ai_exam_prompt(raw_text, job_context)
    for candidate in candidates:
        provider = {**candidate, "_fallback_providers": []}
        label = f"{provider.get('name') or provider.get('id')} / {provider.get('model') or 'unknown'}"
        try:
            response = ai.chat(
                [{"role": "user", "content": prompt}],
                system="严格执行分隔协议。证据不足时留空，绝不把面试官话语写入候选人回答。",
                max_tokens=9000, provider=provider, timeout=180,
            )
            bundle = interview_decompose.parse_ai_exam_bundle(response, segments, participants)
            quality = interview_decompose.validate_exam_bundle(bundle, raw_text)
            attempts.append({"provider": label, **quality})
            rejected_bundles.append({
                "provider": label,
                "quality": quality,
                "bundle": bundle,
            })
            if best is None or quality["score"] > best[1]["score"]:
                best = (bundle, quality)
            if quality["accepted"]:
                bundle["quality"] = quality
                bundle["provider"] = label
                return bundle, attempts
        except Exception as exc:
            attempts.append({"provider": label, "accepted": False, "score": 0,
                             "fatal": [str(exc)[:180]], "issues": []})
    if rejected_bundles and primary:
        # Cross-provider calibration: one model may over-merge the interview
        # while another over-splits it. Feed only their structural proposals
        # back to the primary model, together with explicit quality failures,
        # and require a fresh evidence-grounded reconstruction.
        proposals = []
        for item in rejected_bundles:
            proposals.append({
                "provider": item["provider"],
                "quality_issues": item["quality"].get("fatal", []) + item["quality"].get("issues", []),
                "questions": [
                    {
                        "question": question.get("normalized_question") or question.get("original_question"),
                        "type": question.get("question_type"),
                        "stage": (question.get("tags") or {}).get("stage"),
                    }
                    for question in (item["bundle"].get("questions") or [])[:36]
                ],
            })
        repair_prompt = prompt + """

<<<CALIBRATION_FEEDBACK>>>
下面是不同模型的候选结构及其质量问题。它们只是诊断材料，不是事实来源。
请重新阅读上方原始逐字稿，吸收各方案覆盖到的真实主题，但不要复制错误：
1. 产出 7-15 道主问题；连续追问合并到 FOLLOW_UPS，不得压成少数超大主题。
2. 面试官点评、岗位科普、对候选人反问的回答不得单独成为候选人的考题或回答。
3. 每道题必须提供来自原文的短 SOURCE_QUOTE，优先原样摘录。
4. 保持真实面试顺序，并覆盖开场条件、经历深挖、行为/动机、业务判断和反问阶段（仅在原文存在时）。
5. 仍然只返回既定分隔协议，不要解释。

候选结构：
""" + json.dumps(proposals, ensure_ascii=False)
        provider = {**primary, "_fallback_providers": []}
        label = f"{provider.get('name') or provider.get('id')} / {provider.get('model') or 'unknown'}（校准）"
        try:
            response = ai.chat(
                [{"role": "user", "content": repair_prompt}],
                system="你正在修复一次未通过质量门禁的面试重建。严格依据原文和分隔协议，不得臆造。",
                max_tokens=9000, provider=provider, timeout=180,
            )
            bundle = interview_decompose.parse_ai_exam_bundle(response, segments, participants)
            quality = interview_decompose.validate_exam_bundle(bundle, raw_text)
            attempts.append({"provider": label, **quality})
            if quality["accepted"]:
                bundle["quality"] = quality
                bundle["provider"] = label
                return bundle, attempts
        except Exception as exc:
            attempts.append({"provider": label, "accepted": False, "score": 0,
                             "fatal": [str(exc)[:180]], "issues": []})
    if best and best[1]["accepted"]:
        best[0]["quality"] = best[1]
        return best[0], attempts
    return None, attempts


def _upsert_round_role_insights(round_: dict, bundle: dict):
    insights = bundle.get("role_insights") or []
    if not insights:
        return None
    body = ["# 本轮新增岗位认知", "",
            "以下信息来自面试官现场介绍，已与候选人的回答分开保存。", ""]
    for item in insights:
        body += [f"## {item.get('title') or '岗位认知'}", item.get("content") or "", ""]
        if item.get("source_quote"):
            body += [f"**现场证据**：{item['source_quote']}", ""]
    existing = interview_store.list_documents(
        round_id=round_["id"], document_type="role_insight"
    )
    payload = {
        "title": f"第 {round_.get('round_number')} 轮岗位认知增量",
        "body": "\n".join(body),
        "change_summary": "从面试官现场介绍中提取岗位认知",
        "created_by": "ai",
    }
    if existing:
        document, _ = interview_store.update_document(existing[0]["id"], payload)
        return document
    did = interview_store.create_document({
        **payload, "document_type": "role_insight", "scope_type": "track",
        "track_id": round_.get("track_id"), "round_id": round_["id"],
        "folder_key": "role_knowledge", "source_type": "agent",
    })
    return interview_store.get_document(did)


_INTERVIEW_WORKERS: set[int] = set()
_INTERVIEW_WORKERS_LOCK = threading.Lock()


def _retry_sqlite_operation(operation, *, attempts: int = 4):
    """Retry short SQLite write collisions without hiding other failures."""
    for attempt in range(attempts):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if ("locked" not in message and "busy" not in message) or attempt == attempts - 1:
                raise
            time.sleep(0.35 * (attempt + 1))


def _finish_failed_interview_task(task_id: int, run_id: int | None, rid: int, exc: Exception):
    """Record a terminal state even when the first failure write also collides."""
    detail = str(exc)[:300] or exc.__class__.__name__
    updates = []
    if run_id:
        updates.append(lambda: db.update_agent_run(run_id, status="failed", summary=detail))
    updates.extend((
        lambda: db.update_agent_task(task_id, status="failed", result_summary=detail),
        lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "failed",
            "label": "逐字稿拆解失败", "detail": detail, "status": "failed",
        }),
        lambda: interview_store.update_round(rid, {"status": "processing_failed"}),
    ))
    for update in updates:
        try:
            _retry_sqlite_operation(update)
        except Exception:
            # Continue recording the remaining terminal markers. One failed
            # audit write must not leave every object looking active.
            continue


def _run_interview_decomposition_task(task_id: int, rid: int, source_id: int, use_ai: bool = False):
    """Durable background work. The deterministic pass is always available;
    model enrichment is deliberately additive and never touches raw text."""
    task = db.get_agent_task(task_id) or {}
    run_id = None
    try:
        run_id = _retry_sqlite_operation(lambda: db.create_agent_run({
            "task_id": task_id, "run_type": "primary", "expert_key": "review_analyst",
            "model_profile": "fast", "status": "running",
        }))
        _retry_sqlite_operation(lambda: db.update_agent_task(task_id, status="active"))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "running",
            "label": "正在识别说话人与问答", "status": "running",
        }))
        result = _retry_sqlite_operation(lambda: interview_decompose.decompose(rid, source_id))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "draft_ready",
            "label": "已完成本地切分，正在重建完整答卷",
            "detail": "原始材料已经安全归档；接下来识别主问题、连续追问和回答边界",
            "status": "running",
        }))
        source = next((item for item in interview_store.list_transcript_sources(rid) if item.get('id') == source_id), {})
        round_ = interview_store.ensure_round_documents(rid)
        questions = result['questions']
        segments = result['segments']
        parser_note = result.get('parser_note') or "规则切分"
        # ASR often labels every turn as the meeting owner. When that creates a
        # sea of tiny pseudo-questions, use a delimiter-based AI pass to merge
        # them into interview-sized units. Long transcript text never travels
        # through a JSON field.
        # "AI 整理为完整答卷" is a semantic reconstruction request, not a
        # last-resort fallback for a large number of questions. The local pass
        # only protects raw material and provides a conservative draft.
        model_bundle = None
        if use_ai:
            try:
                _retry_sqlite_operation(lambda: db.create_agent_event({
                    "task_id": task_id, "run_id": run_id, "event_type": "model_running",
                    "label": "正在进行语义重建与质量校验",
                    "detail": "模型正在整理整场节奏、问题意图和真实回答；可以离开本页继续使用 Caddie",
                    "status": "running",
                }))
                track = db.get_job_track(round_.get("track_id")) or {}
                job_context = "\n".join(x for x in (
                    f"公司：{track.get('company')}" if track.get("company") else "",
                    f"岗位：{track.get('role')}" if track.get("role") else "",
                    f"JD：{track.get('jd')}" if track.get("jd") else "",
                ) if x)
                model_bundle, attempts = _validated_exam_from_models(
                    source.get("raw_text") or "", segments, result["participants"], job_context
                )
                for attempt in attempts:
                    detail = "；".join((attempt.get("fatal") or []) + (attempt.get("issues") or []))
                    _retry_sqlite_operation(lambda attempt=attempt, detail=detail: db.create_agent_event({
                        "task_id": task_id, "run_id": run_id, "event_type": "model_quality",
                        "label": f"{attempt.get('provider')}：{'通过' if attempt.get('accepted') else '未采用'}",
                        "detail": f"质量分 {attempt.get('score', 0)}" + (f"；{detail}" if detail else ""),
                        "status": "done" if attempt.get("accepted") else "warning",
                    }))
                if model_bundle:
                    _retry_sqlite_operation(
                        lambda: interview_store.replace_exam(rid, model_bundle["questions"])
                    )
                    questions = interview_store.get_exam(rid)
                    quality = model_bundle.get("quality") or {}
                    parser_note = f"语义重建（{model_bundle.get('provider')}，质量门禁 {quality.get('score', 0)}）"
                    _retry_sqlite_operation(
                        lambda: _upsert_round_role_insights(round_, model_bundle)
                    )
                else:
                    _retry_sqlite_operation(lambda: db.create_agent_event({
                        "task_id": task_id, "run_id": run_id, "event_type": "ai_fallback",
                        "label": "模型输出未通过质量门禁",
                        "detail": "已保留原始资料与本地初稿；不会用低质量结果覆盖答卷",
                        "status": "warning",
                    }))
            except Exception as ai_error:
                # A usable rule-based draft is better than a failed upload.
                parser_note = f"规则切分（语义重建未完成：{str(ai_error)[:80]}）"
        _retry_sqlite_operation(
            lambda: _suggest_interview_question_classifications(questions)
        )
        questions = interview_store.get_exam(rid)
        transcript_body = ["# 结构化逐字稿", "", "原始逐字稿保持不变；以下内容可由你修正。", ""]
        for segment in segments:
            speaker = segment.get('display_name') or segment.get('participant_key') or '未识别说话人'
            transcript_body += [f"## {speaker}", segment.get('edited_text') or segment.get('raw_text') or '', '']
        # The answer sheet is always a whole-interview document. A low-quality
        # local pass may still leave some answers as "待确认", but it must not
        # fall back to an unstructured list of fragments.
        answer_body = _build_answer_sheet_markdown(round_, questions, parser_note, model_bundle)
        for field, body, title in (
            ('organized_transcript_document_id', '\n'.join(transcript_body), f"第 {round_.get('round_number')} 轮结构化逐字稿"),
            ('answer_sheet_document_id', answer_body, f"第 {round_.get('round_number')} 轮面试答卷"),
        ):
            did = round_.get(field)
            if did:
                _retry_sqlite_operation(lambda did=did, title=title, body=body:
                    interview_store.update_document(did, {
                        'title': title, 'body': body,
                        'change_summary': '由逐字稿拆解生成初版', 'created_by': 'ai',
                    }))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "questions_created",
            "label": f"已整理 {len(questions)} 道题目",
            "detail": "原始逐字稿保持不变，可在答卷中修正切分和说话人",
            "status": "done",
        }))
        needs = result.get("needs_confirmation")
        summary = f"已形成 {len(questions)} 道结构化题目（{parser_note}）"
        _retry_sqlite_operation(lambda: db.update_agent_run(
            run_id, status="completed", summary=summary,
            output_json=json.dumps({
                "round_id": rid, "question_count": len(questions),
                "needs_confirmation": needs,
            }, ensure_ascii=False),
        ))
        _retry_sqlite_operation(lambda: db.update_agent_task(
            task_id, status="review" if needs else "completed",
            result_summary=summary + ("；请先确认哪位是本人" if needs else ""),
        ))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "completed",
            "label": "面试答卷初版已完成",
            "detail": ("需要确认后才会进行深度复盘；" if needs else "可以开始逐题复盘；") + parser_note,
            "status": "done",
        }))
        _retry_sqlite_operation(lambda: interview_store.update_round(
            rid, {"status": "processing" if needs else "review_ready"}
        ))
        _commit("完成面试逐字稿拆解")
    except Exception as exc:
        _finish_failed_interview_task(task_id, run_id, rid, exc)
    finally:
        with _INTERVIEW_WORKERS_LOCK:
            _INTERVIEW_WORKERS.discard(task_id)


def _schedule_interview_decomposition(task_id: int, rid: int, source_id: int,
                                      use_ai: bool = False) -> bool:
    """Start one process-local worker while the durable task remains in SQLite."""
    with _INTERVIEW_WORKERS_LOCK:
        if task_id in _INTERVIEW_WORKERS:
            return False
        _INTERVIEW_WORKERS.add(task_id)
    worker = threading.Thread(
        target=_run_interview_decomposition_task,
        args=(task_id, rid, source_id, use_ai),
        name=f"interview-decompose-{task_id}",
        daemon=True,
    )
    worker.start()
    return True


def _recover_interview_decomposition_tasks():
    """Resume work lost when the local server was restarted mid-analysis."""
    for task in db.list_agent_tasks_for_recovery("interview_decompose"):
        try:
            context_data = json.loads(task.get("context_json") or "{}")
            source_id = int(context_data["source_id"])
            rid = int(task["object_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _finish_failed_interview_task(task["id"], None, task.get("object_id"), exc)
            continue
        for run in db.get_agent_task(task["id"]).get("runs") or []:
            if run.get("status") in {"queued", "running"}:
                _retry_sqlite_operation(lambda run=run: db.update_agent_run(
                    run["id"], status="failed", summary="本地服务重启，任务已自动续跑"
                ))
        _retry_sqlite_operation(lambda task=task: db.update_agent_task(
            task["id"], status="queued", result_summary="本地服务重启，正在自动续跑"
        ))
        _retry_sqlite_operation(lambda task=task: db.create_agent_event({
            "task_id": task["id"], "event_type": "recovered",
            "label": "服务重启后自动续跑", "detail": "无需重新粘贴面试材料",
            "status": "running",
        }))
        _schedule_interview_decomposition(
            task["id"], rid, source_id, bool(context_data.get("use_ai"))
        )


def _finish_failed_interview_question_coach_task(
    task_id: int, run_id: int | None, exc: Exception
):
    detail = str(exc)[:300] or exc.__class__.__name__
    updates = []
    if run_id:
        updates.append(
            lambda: db.update_agent_run(run_id, status="failed", summary=detail)
        )
    updates.extend((
        lambda: db.update_agent_task(
            task_id, status="failed", result_summary=detail
        ),
        lambda: db.create_agent_event({
            "task_id": task_id,
            "run_id": run_id,
            "event_type": "failed",
            "label": "深度批改失败",
            "detail": detail,
            "status": "failed",
        }),
    ))
    for update in updates:
        try:
            _retry_sqlite_operation(update)
        except Exception:
            continue


def _run_interview_question_coach_task(
    task_id: int, question_id: int, instruction: str | None
):
    run_id = None
    try:
        run_id = _retry_sqlite_operation(lambda: db.create_agent_run({
            "task_id": task_id,
            "run_type": "primary",
            "expert_key": "review_analyst",
            "model_profile": "deep_reasoning",
            "status": "running",
        }))
        _retry_sqlite_operation(
            lambda: db.update_agent_task(task_id, status="active")
        )
        question = interview_store.get_question(question_id)
        if not question:
            raise RuntimeError("面试题目不存在")
        workspace = question.get("answer_workspace") or {}
        source_answer = str(
            workspace.get("current_answer")
            or workspace.get("evidence_answer")
            or ""
        ).strip()
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id,
            "run_id": run_id,
            "event_type": "question_context",
            "label": "正在读取题目、岗位与真实回答",
            "detail": "已锁定当前答案版本，后台任务不会覆盖原始逐字稿",
            "status": "running",
        }))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id,
            "run_id": run_id,
            "event_type": "question_intent",
            "label": "正在还原面试官意图与追问链",
            "detail": "结合题目前后文、目标岗位和关联经历判断考察重点",
            "status": "running",
        }))
        coaching, model = _generate_interview_question_coaching(
            question, source_answer, instruction
        )
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id,
            "run_id": run_id,
            "event_type": "question_diagnosis",
            "label": "已完成答案诊断，正在整理批改文档",
            "detail": "保留有效事实，区分表达、逻辑、证据和岗位匹配问题",
            "status": "running",
        }))
        version = _retry_sqlite_operation(
            lambda: interview_store.save_question_coaching_version(
                question_id, {
                    **coaching,
                    "source_answer_version": workspace.get("current_version") or 0,
                    "instruction": instruction,
                    **model,
                }
            )
        )
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id,
            "run_id": run_id,
            "event_type": "question_saved",
            "label": "批改版本已保存",
            "detail": f"已生成逐题批改 v{version.get('version') or 1}",
            "status": "done",
        }))
        summary = f"逐题批改 v{version.get('version') or 1} 已完成"
        _retry_sqlite_operation(lambda: db.update_agent_run(
            run_id,
            status="completed",
            provider_id=model.get("model_provider"),
            model_name=model.get("model_name"),
            output_json=json.dumps({
                "question_id": question_id,
                "coaching_version": version.get("version"),
            }, ensure_ascii=False),
            summary=summary,
        ))
        _retry_sqlite_operation(lambda: db.update_agent_task(
            task_id, status="completed", result_summary=summary
        ))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id,
            "run_id": run_id,
            "event_type": "completed",
            "label": "深度批改已完成",
            "detail": "结果已保存，可随时返回题目查看和继续讨论",
            "status": "done",
        }))
        _commit("完成面试真题深度批改")
    except Exception as exc:
        _finish_failed_interview_question_coach_task(task_id, run_id, exc)
    finally:
        with _INTERVIEW_WORKERS_LOCK:
            _INTERVIEW_WORKERS.discard(task_id)


def _schedule_interview_question_coach(
    task_id: int, question_id: int, instruction: str | None
) -> bool:
    with _INTERVIEW_WORKERS_LOCK:
        if task_id in _INTERVIEW_WORKERS:
            return False
        _INTERVIEW_WORKERS.add(task_id)
    threading.Thread(
        target=_run_interview_question_coach_task,
        args=(task_id, question_id, instruction),
        name=f"interview-question-coach-{task_id}",
        daemon=True,
    ).start()
    return True


def _recover_interview_question_coach_tasks():
    for task in db.list_agent_tasks_for_recovery("interview_question_coach"):
        try:
            context_data = json.loads(task.get("context_json") or "{}")
            question_id = int(context_data["question_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            _finish_failed_interview_question_coach_task(task["id"], None, exc)
            continue
        full_task = db.get_agent_task(task["id"]) or {}
        for run in full_task.get("runs") or []:
            if run.get("status") in {"queued", "running"}:
                _retry_sqlite_operation(lambda run=run: db.update_agent_run(
                    run["id"],
                    status="failed",
                    summary="本地服务重启，任务已自动续跑",
                ))
        _retry_sqlite_operation(lambda task=task: db.update_agent_task(
            task["id"],
            status="queued",
            result_summary="本地服务重启，正在自动续跑",
        ))
        _retry_sqlite_operation(lambda task=task: db.create_agent_event({
            "task_id": task["id"],
            "event_type": "recovered",
            "label": "服务重启后自动续跑",
            "detail": "无需重新提交题目或答案",
            "status": "running",
        }))
        _schedule_interview_question_coach(
            task["id"], question_id, context_data.get("instruction")
        )


@app.post("/api/v2/interview-rounds/{rid}/decompose")
def decompose_interview_transcript_v2(rid: int, body: dict, background_tasks: BackgroundTasks):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    source_id = body.get("source_id")
    if not source_id:
        raise HTTPException(400, "需要指定 source_id")
    if not any(item.get("id") == int(source_id) for item in interview_store.list_transcript_sources(rid)):
        raise HTTPException(404, "逐字稿资料不存在")
    round_ = interview_store.get_round(rid)
    use_ai = bool(body.get("use_ai", False))
    task_id = db.create_agent_task({"task_type":"interview_decompose", "title":f"拆解第{round_.get('round_number')}轮面试逐字稿", "instruction":"识别参与者、切分问答并生成可编辑答卷" + ("（AI 深度归并）" if use_ai else ""), "object_type":"interview_round", "object_id":rid, "track_id":round_.get("track_id"), "status":"queued", "assigned_expert":"review_analyst", "context_json":json.dumps({"source_id":int(source_id),"use_ai":use_ai},ensure_ascii=False)})
    db.create_agent_event({"task_id":task_id,"event_type":"queued","label":"逐字稿已进入拆解队列","detail":"你可以离开当前页面，完成后会显示在本轮面试中","status":"pending"})
    interview_store.update_round(rid, {"status": "processing"})
    _schedule_interview_decomposition(task_id, rid, int(source_id), use_ai)
    _commit("开始拆解面试逐字稿")
    return {"ok": True, "data": {"task_id":task_id,"round_id":rid,"status":"queued"}}


@app.get("/api/v2/interview-tasks/{task_id}")
def get_interview_task_v2(task_id: int):
    task = db.get_agent_task(task_id)
    if not task or task.get("task_type") not in {
        "interview_decompose", "interview_review", "round_preparation",
        "interview_question_coach", "interview_growth_analysis",
    }:
        raise HTTPException(404,"面试任务不存在")
    return {"ok":True,"data":_interview_task_progress(task)}


@app.get("/api/v2/interview-rounds/{rid}/exam")
def get_interview_exam_v2(rid: int):
    if not interview_store.get_round(rid):
        raise HTTPException(404, "面试轮次不存在")
    return {"ok": True, "items": interview_store.get_exam(rid)}


def _baseline_question_review(question: dict) -> dict:
    answer = next((x for x in question.get("answers") or [] if x.get("is_self")), None)
    text = ((answer or {}).get("organized_answer") or (answer or {}).get("original_answer") or "").strip()
    is_reverse = question.get("question_type") == "反问"
    evidence_ids = _json_list((answer or {}).get("evidence_segment_ids_json"))
    score = 3.0 if text else (3.0 if is_reverse else 2.0)
    return {
        "question_id": question["id"], "answer_id": (answer or {}).get("id"),
        "directness_score": score, "structure_score": score,
        "evidence_score": 3.0 if evidence_ids else 2.0,
        "relevance_score": score, "credibility_score": 3.0 if evidence_ids else 2.2,
        "overall_score": score,
        "strengths": "等待语义复盘确认。" if text or is_reverse else "未可靠识别到本人回答。",
        "weaknesses": "模型复盘不可用，当前不根据字数推断回答质量。",
        "better_approach": "回到问题意图与现场证据，补齐结论、依据、本人动作和结果边界。",
        "better_answer": "", "evidence_segment_ids": evidence_ids,
        "inference_level": "inferred",
    }


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or '[]')
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _enrich_review_with_model(questions: list[dict], reviews: list[dict],
                              role_context: str = "") -> tuple[list[dict], dict | None]:
    """Build a review through small, independently validated model calls.

    Whole-interview judgment and per-question grading have different grains.
    Splitting them also prevents a long JSON response from being truncated:
    models may phrase findings differently, while the harness guarantees the
    same reviewed artifacts and quality contract.
    """
    compact = []
    for q in questions:
        answer = next((a for a in q.get('answers') or [] if a.get('is_self')), {})
        tags = q.get("tags") if isinstance(q.get("tags"), dict) else {}
        compact.append({"question_id": q['id'], "question": q.get('normalized_question') or q.get('original_question'),
                        "question_type": q.get("question_type") or "",
                        "ability_key": q.get("ability_key") or "",
                        "intent": q.get("intent") or "",
                        "stage": tags.get("stage") or "",
                        "answer": (answer.get('organized_answer') or answer.get('original_answer') or '')[:1600],
                        "interviewer_signal": answer.get('interviewer_signal') or ''})
    shared_rules = """你是严谨的面试复盘分析师。
规则：
1. 仅根据给出的题目、候选人回答、连续追问和现场信号判断。
2. 禁止把没有 HR 明确反馈的失败原因写成事实；证据不足必须标明“推断”。
3. 不得按回答长度打分。不同题型使用不同标准：项目题看问题定义、本人动作、决策依据、结果边界；动机题看选择逻辑和稳定性；业务题看框架完整性与岗位相关性；行为题看真实情境、取舍与结果；反问看问题质量，不要求候选人回答。
4. strengths、weaknesses、better_approach 必须针对该题事实，禁止所有题重复同一句。
5. 通过预测必须引用正负现场证据。不能因“识别到若干回答”直接推算概率。
6. better_answer 只能重组已出现的事实，不得补写输入中没有的行动、数据、结论或承诺；需要新增事实时只写入 better_approach。
7. 只返回指定 JSON，不要 Markdown，不要前后解释。"""
    resolved = ai.resolve_model_profile("deep_reasoning")
    primary = resolved.get("provider") or {}
    candidates = [primary] + list(primary.get("_fallback_providers") or [])

    def call_json(system: str, payload: object, validator, max_tokens: int = 2800):
        for candidate in candidates:
            try:
                provider = {**candidate, "_fallback_providers": []}
                raw = ai.chat(
                    [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    system=system, max_tokens=max_tokens, provider=provider, timeout=180
                )
                data = ai.extract_json(raw)
                if validator(data):
                    label = f"{provider.get('name') or provider.get('id')} / {provider.get('model') or 'unknown'}"
                    return data, label
            except Exception:
                continue
        return None, None

    overall_prompt = shared_rules + """
任务：从整场题目顺序、连续追问和回答变化中，还原面试官的判断路径，给出整场结论与通过预测。不要逐题批改。
返回格式：
{
  "overall":{
    "summary":"整场表现结论",
    "judgment_path":"按时间说明面试官如何从基础条件、经历验证、风险确认推进判断",
    "strengths":["有现场证据的优势"],
    "risks":["有现场证据或明确标为推断的风险"],
    "stage_findings":[{"stage":"阶段","finding":"判断","evidence":"对应题目或回答事实"}]
  },
  "prediction":{
    "label":"likely_pass/borderline/likely_fail",
    "probability":0,
    "confidence":"low/medium",
    "positive_evidence":["现场证据"],
    "negative_evidence":["现场证据"],
    "uncertainty":["缺失信息"]
  }
}
probability 永远表示通过概率：likely_pass 为 60-95，borderline 为 40-59，likely_fail 为 5-39。"""
    if role_context:
        overall_prompt += "\n岗位上下文：\n" + role_context[:4500]
    overall_payload = [
        {**item, "answer": item.get("answer", "")[:800]}
        for item in compact
    ]

    def valid_overall(data):
        overall = data.get("overall") if isinstance(data, dict) else None
        prediction = data.get("prediction") if isinstance(data, dict) else None
        if not isinstance(overall, dict) or not isinstance(prediction, dict):
            return False
        label = prediction.get("label")
        probability = prediction.get("probability")
        return (
            len((overall.get("summary") or "").strip()) >= 20
            and len((overall.get("judgment_path") or "").strip()) >= 35
            and label in {"likely_pass", "borderline", "likely_fail"}
            and isinstance(probability, (int, float))
            and bool(prediction.get("positive_evidence") or prediction.get("negative_evidence"))
        )

    overall_data, overall_provider = call_json(
        overall_prompt, overall_payload, valid_overall, max_tokens=2400
    )

    question_prompt = shared_rules + """
任务：逐题批改当前小批次。每道输入题必须返回一项；不要输出整场总结或通过预测。
返回格式：
{
  "questions":[{
    "question_id":1,
    "question_type":"基础信息/项目深挖/业务理解/行为面试/动机/反问/其他",
    "ability_key":"考察能力",
    "intent":"面试官真正想验证什么",
    "evaluation_criteria":"好回答的判定标准",
    "scores":{"directness":1,"structure":1,"evidence":1,"relevance":1,"credibility":1},
    "strengths":"针对该回答的具体事实",
    "weaknesses":"针对该回答的具体问题或谨慎推断",
    "better_approach":"下一次的回答路径",
    "better_answer":"仅使用原回答事实重组；无法可靠重组则留空"
  }],
  "actions":[{"question_id":1,"title":"行动标题","detail":"需要补什么","priority":"high/medium/low"}]
}"""
    if role_context:
        question_prompt += "\n岗位上下文：\n" + role_context[:3000]

    reviewed_items = []
    actions = []
    question_providers = []
    for offset in range(0, len(compact), 4):
        batch = compact[offset:offset + 4]
        expected = {item["question_id"] for item in batch}

        def valid_batch(data, expected_ids=expected):
            items = data.get("questions") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return False
            by_id = {
                item.get("question_id"): item for item in items
                if isinstance(item, dict) and item.get("question_id") in expected_ids
            }
            if set(by_id) != expected_ids:
                return False
            for item in by_id.values():
                scores = item.get("scores")
                if not isinstance(scores, dict):
                    return False
                if not all(isinstance(scores.get(key), (int, float))
                           for key in ("directness", "structure", "evidence", "relevance", "credibility")):
                    return False
                if not (item.get("strengths") or "").strip():
                    return False
                if not (item.get("weaknesses") or "").strip():
                    return False
                if not (item.get("better_approach") or "").strip():
                    return False
            return True

        batch_data, batch_provider = call_json(
            question_prompt, batch, valid_batch, max_tokens=3000
        )
        if batch_data:
            reviewed_items.extend(batch_data.get("questions") or [])
            actions.extend(batch_data.get("actions") or [])
            if batch_provider:
                question_providers.append(batch_provider)

    covered_ids = {item.get("question_id") for item in reviewed_items if isinstance(item, dict)}
    minimum_coverage = max(1, int(len(compact) * .7))
    if len(covered_ids) < minimum_coverage:
        return reviews, None
    by_id = {item.get('question_id'): item for item in reviewed_items if isinstance(item, dict)}
    question_by_id = {item["id"]: item for item in questions}

    def grounded_rewrite(question_id, candidate):
        """Keep a rewrite only when it is recognisably grounded in the answer.

        Full invented model answers are more harmful than an empty field. The
        product still retains the model's improvement path when a rewrite is
        suppressed.
        """
        candidate = (candidate or "").strip()
        question = question_by_id.get(question_id) or {}
        answer = next((item for item in question.get("answers") or [] if item.get("is_self")), {})
        source = (answer.get("organized_answer") or answer.get("original_answer") or "").strip()
        if not candidate or not source:
            return ""
        unsupported_assertions = ("不打算", "从未", "一定会", "完全由我", "唯一原因", "全部负责")
        if any(term in candidate and term not in source for term in unsupported_assertions):
            return ""
        compact_source = re.sub(r"\W+", "", source)
        compact_candidate = re.sub(r"\W+", "", candidate)
        if len(compact_candidate) > max(120, len(compact_source) * 2):
            return ""
        source_grams = {compact_source[i:i + 2] for i in range(max(0, len(compact_source) - 1))}
        candidate_grams = {compact_candidate[i:i + 2] for i in range(max(0, len(compact_candidate) - 1))}
        overlap = len(source_grams & candidate_grams) / max(1, len(candidate_grams))
        return candidate if overlap >= .22 else ""

    output = []
    for review in reviews:
        extra = by_id.get(review['question_id']) or {}
        scores = extra.get('scores') or {}
        enriched = {**review,
            'directness_score': scores.get('directness', review['directness_score']),
            'structure_score': scores.get('structure', review['structure_score']),
            'evidence_score': scores.get('evidence', review['evidence_score']),
            'relevance_score': scores.get('relevance', review['relevance_score']),
            'credibility_score': scores.get('credibility', review['credibility_score']),
            'strengths': extra.get('strengths') or review['strengths'],
            'weaknesses': extra.get('weaknesses') or review['weaknesses'],
            'better_approach': extra.get('better_approach') or review['better_approach'],
            'better_answer': grounded_rewrite(review['question_id'], extra.get('better_answer')),
            'inference_level': 'inferred'}
        values = [enriched[k] for k in ('directness_score', 'structure_score', 'evidence_score', 'relevance_score', 'credibility_score') if isinstance(enriched[k], (int, float))]
        enriched['overall_score'] = round(sum(values) / len(values), 1) if values else review['overall_score']
        output.append(enriched)
        if extra:
            interview_store.update_question_enrichment(review['question_id'], {
                'question_type': extra.get('question_type'), 'ability_key': extra.get('ability_key'),
                'intent': extra.get('intent'), 'evaluation_criteria': extra.get('evaluation_criteria')})
    providers = []
    for label in [overall_provider, *question_providers]:
        if label and label not in providers:
            providers.append(label)
    return output, {
        "prediction": (overall_data or {}).get("prediction"),
        "actions": actions,
        "overall": (overall_data or {}).get("overall") or {},
        "provider": "；".join(providers) or None,
    }


def _render_review_report(round_: dict, questions: list[dict], reviews: list[dict],
                          prediction: dict, overall: dict | None = None) -> str:
    rows = {x["question_id"]: x for x in reviews}
    overall = overall or {}
    blocks = [f"# 第 {round_.get('round_number')} 轮面试复盘", "",
              "## 一、本轮结论",
              overall.get("summary") or "当前仅形成基础复盘，需结合逐字稿与岗位上下文继续确认。",
              "",
              f"- 通过判断：{prediction['label']}",
              f"- 通过概率：{prediction.get('probability', 0)}%",
              f"- 判断信心：{prediction.get('confidence')}", "- 说明：以下结论区分逐字稿可见事实与基于回答的推断；没有 HR 反馈时，不把失败原因写成事实。", "",
              "## 二、面试官的判断路径",
              overall.get("judgment_path") or "尚未获得足够可靠的语义分析。",
              ""]
    stage_findings = overall.get("stage_findings") or []
    if stage_findings:
        blocks += ["## 三、分阶段表现", ""]
        for item in stage_findings:
            blocks += [f"### {item.get('stage') or '阶段'}", item.get("finding") or "",
                       f"**现场证据**：{item.get('evidence') or '待确认'}", ""]
    blocks += ["## 四、整场优势与风险", "",
               "### 已验证优势",
               *([f"- {item}" for item in overall.get("strengths") or []] or ["- 待进一步确认。"]),
               "", "### 主要风险",
               *([f"- {item}" for item in overall.get("risks") or []] or ["- 待进一步确认。"]),
               "", "## 五、逐题考点解析", ""]
    segments = {x['id']: x for x in interview_store.list_segments(round_['id'])}
    def evidence(ids):
        lines=[]
        for sid in ids or []:
            item=segments.get(sid)
            if item:
                speaker=item.get('display_name') or item.get('participant_key') or '说话人'
                text=(item.get('edited_text') or item.get('raw_text') or '').strip().replace('\n',' ')
                lines.append(f"- [{speaker} / 片段 {sid}] {text[:180]}")
        return lines or ["- 未定位到可引用的逐字稿片段，需人工确认。"]
    for question in questions:
        review=rows.get(question['id'],{})
        answer=next((x for x in question.get('answers') or [] if x.get('is_self')), {})
        blocks += [f"### Q{question.get('question_order')}：{question.get('normalized_question') or question.get('original_question')}",
                   "", "**我的原回答**", answer.get('organized_answer') or answer.get('original_answer') or "（未识别到本人回答，需人工确认）", "",
                   "**面试官意图**", question.get('intent') or "待结合岗位和上下文进一步判断。", "",
                   "**证据依据**", *evidence(review.get('evidence_segment_ids') or _json_list(answer.get('evidence_segment_ids_json'))), "",
                   "**批改**", f"- 综合评分：{review.get('overall_score','-')}/5", f"- 做得好：{review.get('strengths','-')}", f"- 待改进：{review.get('weaknesses','-')}", f"- 下次思路：{review.get('better_approach','-')}", ""]
        if review.get("better_answer"):
            blocks += ["**参考重组**", review["better_answer"], ""]
    return '\n'.join(blocks)


def _upsert_interview_review_knowledge(round_: dict, report: str) -> dict:
    folders, _ = db.ensure_track_knowledge_folders(round_["track_id"])
    folder = next((item for item in folders if item.get("name") == "面试复盘"), None)
    if not folder:
        raise RuntimeError("岗位准备知识缺少面试复盘目录")
    existing = next((
        item for item in db.list_knowledge_items(
            track_id=round_["track_id"], folder_id=folder["id"], include_archived=True
        )
        if item.get("source_type") == "interview_review"
        and str(item.get("source_ref_id")) == str(round_["id"])
    ), None)
    payload = {
        "title": f"第 {round_.get('round_number')} 轮面试复盘",
        "content": report,
        "scope_type": "track",
        "track_id": round_["track_id"],
        "folder_id": folder["id"],
        "topic": "面试复盘",
        "mastery": "learning",
        "status": "active",
    }
    if existing:
        db.update_knowledge_item(existing["id"], payload)
        return db.get_knowledge_item(existing["id"])
    kid = db.create_knowledge_item({
        **payload,
        "source_type": "interview_review",
        "source_ref_id": round_["id"],
    })
    return db.get_knowledge_item(kid)


def _review_action_target(question: dict | None, review_knowledge: dict) -> dict:
    links = sorted(
        (question or {}).get("entity_links") or [],
        key=lambda item: (
            int(bool(item.get("confirmed"))),
            float(item.get("confidence") or 0),
        ),
        reverse=True,
    )
    for link in links:
        entity_type = link.get("entity_type")
        entity_id = link.get("entity_id")
        if entity_type == "project" and db.get_project(entity_id):
            return {"target_type": "project", "target_id": entity_id,
                    "target_label": link.get("entity_title") or "关联项目",
                    "target_kind_label": "我的项目"}
        if entity_type == "experience" and db.get_experience(entity_id):
            return {"target_type": "experience", "target_id": entity_id,
                    "target_label": link.get("entity_title") or "关联经历",
                    "target_kind_label": "我的经历"}
        if entity_type == "knowledge_item" and db.get_knowledge_item(entity_id):
            return {"target_type": "knowledge_item", "target_id": entity_id,
                    "target_label": link.get("entity_title") or "准备知识",
                    "target_kind_label": "准备知识"}
    return {
        "target_type": "knowledge_item",
        "target_id": review_knowledge["id"],
        "target_label": review_knowledge["title"],
        "target_kind_label": "面试复盘",
    }


def _ensure_interview_review_artifacts(round_: dict) -> dict | None:
    """Backfill knowledge placement and concrete action targets for old reviews."""
    review_id = round_.get("review_document_id")
    review_document = interview_store.get_document(review_id) if review_id else None
    if not review_document or not (review_document.get("body") or "").strip():
        return None
    review_knowledge = _upsert_interview_review_knowledge(
        round_, review_document.get("body") or ""
    )
    questions = {
        item["id"]: item for item in interview_store.get_exam(round_["id"])
    }
    for action in interview_store.list_review_actions(round_["id"]):
        payload = action.get("proposed_payload") or {}
        target_id = action.get("target_id")
        target_type = action.get("target_type")
        target_exists = (
            target_type == "project" and target_id and db.get_project(target_id)
        ) or (
            target_type == "experience" and target_id and db.get_experience(target_id)
        ) or (
            target_type == "knowledge_item" and target_id
            and db.get_knowledge_item(target_id)
        )
        if target_exists and payload.get("target_label"):
            continue
        target = _review_action_target(
            questions.get(action.get("question_id")), review_knowledge
        )
        interview_store.set_review_action_target(
            action["id"],
            target["target_type"],
            target["target_id"],
            {
                **payload,
                **target,
                "review_knowledge_item_id": review_knowledge["id"],
            },
        )
    return review_knowledge


def _run_interview_review_task(task_id: int, rid: int):
    run_id = None
    try:
        run_id = _retry_sqlite_operation(lambda: db.create_agent_run({
            "task_id": task_id, "run_type": "primary", "expert_key": "review_analyst",
            "model_profile": "deep_reasoning", "status": "running",
        }))
        _retry_sqlite_operation(lambda: db.update_agent_task(task_id, status="active"))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "review_started",
            "label": "正在读取结构化答卷", "status": "running",
        }))
        round_=interview_store.get_round(rid); questions=interview_store.get_exam(rid)
        if not round_ or not questions:
            raise RuntimeError("面试轮次或结构化答卷不存在")
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "review_questions_ready",
            "label": f"已读取 {len(questions)} 道题，开始语义复盘", "status": "running",
        }))
        reviews=[_baseline_question_review(q) for q in questions]
        track = db.get_job_track(round_.get("track_id")) or {}
        role_context = "\n".join(x for x in (
            f"公司：{track.get('company')}" if track.get("company") else "",
            f"岗位：{track.get('role')}" if track.get("role") else "",
            f"JD：{track.get('jd')}" if track.get("jd") else "",
        ) if x)
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "review_model_running",
            "label": "正在还原面试官判断路径并逐题批改", "status": "running",
        }))
        reviews, model_output = _enrich_review_with_model(questions, reviews, role_context)
        _retry_sqlite_operation(lambda: interview_store.replace_question_reviews(rid,reviews))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "model_quality",
            "label": "语义复盘已通过结构校验", "status": "running",
        }))
        model_prediction = (model_output or {}).get('prediction') or {}
        model_probability = model_prediction.get('probability')
        label = model_prediction.get('label') if model_prediction.get('label') in {'likely_pass','borderline','likely_fail'} else "borderline"
        probability = max(5, min(95, model_probability)) if isinstance(model_probability,(int,float)) else 50
        # Some providers interpret probability as confidence in the textual
        # label. Persist one unambiguous metric: probability of passing.
        if label == "likely_fail" and probability > 50:
            probability = 100 - probability
        elif label == "likely_pass" and probability < 50:
            probability = 100 - probability
        elif label == "borderline":
            probability = max(40, min(59, probability))
        prediction={
            "label": label,
            "probability": probability,
            "confidence": model_prediction.get('confidence') if model_prediction.get('confidence') in {'low','medium'} else "low",
            "positive_evidence": model_prediction.get('positive_evidence') or ["模型语义复盘未通过质量门禁，暂不推断正向信号"],
            "negative_evidence": model_prediction.get('negative_evidence') or ["当前没有足够可靠的整场判断证据"],
            "uncertainty": model_prediction.get('uncertainty') or (["模型复盘不可用，50%仅表示暂不判断"] if not model_output else ["未收到 HR 明确反馈，预测仅作辅助"]),
            "model_provider": (model_output or {}).get("provider"),
            "prompt_version": "interview-review-v3",
        }
        _retry_sqlite_operation(lambda: interview_store.create_prediction(rid,prediction))
        report=_render_review_report(round_,questions,reviews,prediction,(model_output or {}).get("overall"))
        existing_review_id = round_.get('review_document_id')
        if existing_review_id and interview_store.get_document(existing_review_id):
            updated, _ = _retry_sqlite_operation(lambda: interview_store.update_document(existing_review_id, {"title":f"第 {round_.get('round_number')} 轮面试复盘", "body":report, "change_summary":"基于最新答卷更新复盘", "created_by":"ai"}))
            did = updated['id']
        else:
            did=_retry_sqlite_operation(lambda: interview_store.create_document({"title":f"第 {round_.get('round_number')} 轮面试复盘", "document_type":"review_report", "body":report,"scope_type":"track","track_id":round_.get('track_id'),"round_id":rid,"source_type":"agent","change_summary":"基于结构化答卷生成复盘初版","created_by":"ai"}))
        review_knowledge = _retry_sqlite_operation(lambda: _upsert_interview_review_knowledge(round_, report))
        _retry_sqlite_operation(lambda: interview_store.update_round(rid,{"review_document_id":did,"status":"awaiting_result"}))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "review_document_saved",
            "label": "复盘文档已保存到准备知识 / 面试复盘", "status": "running",
        }))
        question_by_id = {item["id"]: item for item in questions}
        actions=[]
        for action in (model_output or {}).get('actions') or []:
            if isinstance(action, dict) and action.get('title'):
                target = _review_action_target(question_by_id.get(action.get("question_id")), review_knowledge)
                actions.append({'question_id': action.get('question_id'), 'action_type':'add_training',
                    'target_type': target["target_type"], 'target_id': target["target_id"],
                    'title':action['title'],'detail':action.get('detail'),'priority':action.get('priority') or 'medium',
                    'proposed_payload': {**target, "review_knowledge_item_id": review_knowledge["id"]}})
        for q,review in zip(questions,reviews):
            if review['overall_score'] < 3.2:
                target = _review_action_target(q, review_knowledge)
                actions.append({"question_id":q['id'],"action_type":"add_training",
                    "target_type": target["target_type"], "target_id": target["target_id"],
                    "title":f"补强：{q.get('normalized_question') or q.get('original_question')}",
                    "detail":review['better_approach'],"priority":"high",
                    "proposed_payload": {**target, "review_knowledge_item_id": review_knowledge["id"]}})
        _retry_sqlite_operation(lambda: interview_store.replace_review_actions(rid,actions[:5]))
        _retry_sqlite_operation(lambda: db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "review_actions_ready",
            "label": "已将下一步行动关联到具体项目或知识文档", "status": "running",
        }))
        summary=f"已完成 {len(questions)} 道题目的初版复盘"
        _retry_sqlite_operation(lambda: db.update_agent_run(run_id,status="completed",summary=summary,output_json=json.dumps({"review_document_id":did,"knowledge_item_id":review_knowledge["id"]},ensure_ascii=False)))
        _retry_sqlite_operation(lambda: db.update_agent_task(task_id,status="review",result_summary=summary))
        _retry_sqlite_operation(lambda: db.create_agent_event({"task_id":task_id,"run_id":run_id,"event_type":"completed","label":"复盘初版已完成","detail":"复盘已归档，行动项已关联具体资料","status":"done"}))
        _commit("完成面试复盘初版")
    except Exception as exc:
        if run_id:
            _retry_sqlite_operation(lambda: db.update_agent_run(run_id,status="failed",summary=str(exc)[:300]))
        _retry_sqlite_operation(lambda: db.update_agent_task(task_id,status="failed",result_summary=str(exc)[:300]))
        _retry_sqlite_operation(lambda: db.create_agent_event({"task_id":task_id,"run_id":run_id,"event_type":"failed","label":"复盘生成失败","detail":str(exc)[:300],"status":"failed"}))
    finally:
        with _INTERVIEW_WORKERS_LOCK:
            _INTERVIEW_WORKERS.discard(task_id)


def _schedule_interview_review(task_id: int, rid: int):
    with _INTERVIEW_WORKERS_LOCK:
        if task_id in _INTERVIEW_WORKERS:
            return
        _INTERVIEW_WORKERS.add(task_id)
    threading.Thread(
        target=_run_interview_review_task,
        args=(task_id, rid),
        name=f"interview-review-{task_id}",
        daemon=True,
    ).start()


def _recover_interview_review_tasks():
    for task in db.list_agent_tasks_for_recovery("interview_review"):
        task_id = task["id"]
        rid = int(task.get("object_id") or 0)
        if not rid or not interview_store.get_round(rid):
            continue
        full_task = db.get_agent_task(task_id) or task
        for run in full_task.get("runs") or []:
            if run.get("status") == "running":
                db.update_agent_run(run["id"], status="failed", summary="本地服务重启，任务已自动续跑")
        db.update_agent_task(task_id, status="queued", result_summary="本地服务重启，正在自动续跑")
        db.create_agent_event({
            "task_id": task_id, "event_type": "recovered",
            "label": "本地服务重启，正在续跑面试复盘", "status": "running",
        })
        _schedule_interview_review(task_id, rid)


@app.post("/api/v2/interview-rounds/{rid}/review")
def create_interview_review_v2(rid: int):
    round_=interview_store.get_round(rid)
    if not round_: raise HTTPException(404,"面试轮次不存在")
    if not interview_store.get_exam(rid): raise HTTPException(400,"请先完成逐字稿拆解")
    existing = next((
        item for item in db.list_agent_tasks(
            object_type="interview_round", object_id=rid, limit=50
        )
        if item.get("task_type") == "interview_review"
        and item.get("status") in {"queued", "active"}
    ), None)
    if existing:
        _schedule_interview_review(existing["id"], rid)
        return {"ok":True,"data":{"task_id":existing["id"],"status":existing["status"]}}
    task_id=db.create_agent_task({"task_type":"interview_review","title":f"复盘第{round_.get('round_number')}轮面试", "instruction":"逐题解析意图、批改回答、生成预测与回流动作", "object_type":"interview_round", "object_id":rid,"track_id":round_.get('track_id'),"status":"queued","assigned_expert":"review_analyst"})
    db.create_agent_event({"task_id":task_id,"event_type":"queued","label":"复盘已进入队列","detail":"可以离开当前页面，完成后会通知你","status":"pending"})
    _schedule_interview_review(task_id,rid);_commit("开始生成面试复盘")
    return {"ok":True,"data":{"task_id":task_id,"status":"queued"}}


@app.get("/api/v2/interview-rounds/{rid}/review")
def get_interview_review_v2(rid: int):
    round_=interview_store.get_round(rid)
    if not round_: raise HTTPException(404,"面试轮次不存在")
    _ensure_interview_review_artifacts(round_)
    return {"ok":True,"data":{"reviews":interview_store.list_question_reviews(rid),"prediction":interview_store.latest_prediction(rid),"actions":interview_store.list_review_actions(rid),"review_document":interview_store.get_document(round_.get('review_document_id')) if round_.get('review_document_id') else None}}


def _growth_role_match(round_item: dict, questions: list[dict], role_family: str,
                       role_subtype: str | None = None) -> tuple[bool, list[str]]:
    """Conservative candidate matching. The user still confirms the scope."""
    role_text = " ".join(str(round_item.get(key) or "") for key in (
        "company", "role", "round_name",
    )).lower()
    question_text = " ".join(
        " ".join(str(question.get(key) or "") for key in (
            "normalized_question", "original_question", "question_type", "ability_key",
        ))
        for question in questions
    ).lower()
    family = (role_family or "").strip().lower()
    subtype = (role_subtype or "").strip().lower()
    reasons: list[str] = []
    if family and family in role_text:
        reasons.append("岗位名称直接匹配")
    if "ai" in family and "产品" in family:
        if ("ai" in role_text or "人工智能" in role_text) and "产品" in role_text:
            reasons.append("岗位名称包含 AI 与产品")
        elif any(keyword in question_text for keyword in (
            "ai产品", "大模型", "agent", "智能体", "rag", "模型能力", "ai coding",
        )) and "产品" in role_text:
            reasons.append("岗位为产品且真题涉及 AI 产品")
    elif family and family in question_text:
        reasons.append("真题内容匹配岗位类别")
    if subtype:
        subtype_terms = [term for term in re.split(r"[、,/\s]+", subtype) if term]
        if subtype_terms and any(term in role_text or term in question_text for term in subtype_terms):
            reasons.append("细分方向匹配")
        elif reasons:
            return False, []
    return bool(reasons), reasons


def _growth_scope_candidates(role_family: str, role_subtype: str | None = None,
                             date_from: str | None = None,
                             date_to: str | None = None) -> list[dict]:
    library = interview_store.interview_library()
    questions_by_round: dict[int, list[dict]] = defaultdict(list)
    for question in library.get("questions") or []:
        questions_by_round[int(question["round_id"])].append(question)
    candidates = []
    for round_item in library.get("rounds") or []:
        date_value = str(round_item.get("scheduled_at") or round_item.get("created_at") or "")[:10]
        if date_from and date_value and date_value < date_from:
            continue
        if date_to and date_value and date_value > date_to:
            continue
        matched, reasons = _growth_role_match(
            round_item, questions_by_round.get(int(round_item["id"]), []),
            role_family, role_subtype,
        )
        if not matched:
            continue
        candidates.append({
            "id": round_item["id"], "track_id": round_item.get("track_id"),
            "company": round_item.get("company"), "role": round_item.get("role"),
            "round_number": round_item.get("round_number"),
            "round_name": round_item.get("round_name"), "date": date_value,
            "status": round_item.get("status"), "actual_result": round_item.get("actual_result"),
            "question_count": round_item.get("question_count") or 0,
            "match_reasons": reasons,
        })
    return candidates


def _growth_answer_text(question: dict) -> str:
    workspace = question.get("answer_workspace") or {}
    for key in ("current_answer", "user_answer", "evidence_answer"):
        value = workspace.get(key)
        if isinstance(value, dict):
            value = value.get("body")
        if str(value or "").strip():
            return str(value).strip()
    for answer in question.get("answers") or []:
        if answer.get("is_self"):
            return str(answer.get("organized_answer") or answer.get("original_answer") or "").strip()
    return ""


def _build_growth_snapshot(round_ids: list[int]) -> dict:
    snapshot = {"schema": "interview-growth-v1", "captured_at": datetime.now().isoformat(timespec="seconds"), "rounds": []}
    for rid in round_ids:
        round_item = interview_store.get_round(int(rid))
        if not round_item:
            continue
        track = db.get_job_track(round_item.get("track_id")) or {}
        reviews = {item["question_id"]: item for item in interview_store.list_question_reviews(int(rid))}
        questions = []
        for question in interview_store.get_exam(int(rid)):
            links = question.get("entity_links") or []
            tags = question.get("tag_items") or []
            review = reviews.get(question["id"]) or {}
            questions.append({
                "id": question["id"], "order": question.get("question_order"),
                "question": question.get("normalized_question") or question.get("original_question"),
                "question_type": question.get("question_type"),
                "ability_key": question.get("ability_key"), "intent": question.get("intent"),
                "answer": _growth_answer_text(question),
                "tags": [item.get("tag_value") for item in tags if item.get("tag_value")],
                "links": [{"type": item.get("entity_type"), "id": item.get("entity_id"),
                           "title": item.get("entity_title"), "aspect": item.get("aspect")}
                          for item in links],
                "review": {key: review.get(key) for key in (
                    "overall_score", "strengths", "weaknesses", "better_approach", "better_answer",
                ) if review.get(key) is not None},
            })
        outcome = interview_store.get_outcome(int(rid)) or {}
        snapshot["rounds"].append({
            "id": round_item["id"], "track_id": round_item.get("track_id"),
            "company": track.get("company"), "role": track.get("role"),
            "round_number": round_item.get("round_number"), "round_name": round_item.get("round_name"),
            "date": str(round_item.get("scheduled_at") or round_item.get("created_at") or "")[:10],
            "status": round_item.get("status"), "actual_result": outcome.get("actual_result"),
            "result_note": outcome.get("user_note") or outcome.get("evidence_text"),
            "questions": questions,
        })
    return snapshot


def _growth_topic(question: dict) -> str:
    tags = [str(item) for item in question.get("tags") or [] if item]
    return (tags[0] if tags else question.get("question_type") or question.get("ability_key") or "综合表达")


def _growth_fallback_sections(analysis: dict, snapshot: dict, mock_count: int) -> dict[str, str]:
    rounds = snapshot.get("rounds") or []
    all_questions = [(round_item, question) for round_item in rounds for question in round_item.get("questions") or []]
    topic_counts: dict[str, int] = defaultdict(int)
    scores: dict[str, list[float]] = defaultdict(list)
    for _, question in all_questions:
        topic = _growth_topic(question)
        topic_counts[topic] += 1
        score = (question.get("review") or {}).get("overall_score")
        if isinstance(score, (int, float)):
            scores[topic].append(float(score))
    evidence_lines = [
        f"- I{index}：{item.get('date') or '日期未知'} · {item.get('company') or ''} · {item.get('role') or ''} · 第 {item.get('round_number') or '?'} 轮 · {len(item.get('questions') or [])} 题"
        for index, item in enumerate(rounds, 1)
    ] or ["- 尚未选择有效面试轮次。"]
    ranked_topics = sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))
    recurring = [item for item in ranked_topics if item[1] >= 2]
    weak_topics = sorted(
        ((topic, sum(values) / len(values)) for topic, values in scores.items() if values),
        key=lambda item: item[1],
    )
    profile = "\n".join((
        f"# {analysis['role_family']}面试能力画像", "",
        "> 本文档基于已选真实面试快照生成。没有逐题评分或结果证据的部分标记为待验证。", "",
        "## 证据范围", *evidence_lines, "",
        "## 高频考察领域",
        *([f"- **{topic}**：出现 {count} 次" for topic, count in ranked_topics] or ["- 暂无可统计题目"]), "",
        "## 当前可验证优势",
        *([f"- **{topic}**：平均复盘得分 {average:.1f}/5（仅基于已有逐题复盘）" for topic, average in sorted(weak_topics, key=lambda item: -item[1])[:3]] or ["- 待完成更多逐题复盘后判断"]), "",
        "## 当前可验证短板",
        *([f"- **{topic}**：平均复盘得分 {average:.1f}/5，需要在下一轮优先验证" for topic, average in weak_topics[:3]] or ["- 缺少统一评分证据，暂不下结论"]),
    ))
    trend_parts = [f"# {analysis['role_family']}面试时间趋势", "", "## 逐场轨迹"]
    for index, item in enumerate(rounds, 1):
        scored = [q.get("review", {}).get("overall_score") for q in item.get("questions") or []]
        scored = [float(value) for value in scored if isinstance(value, (int, float))]
        score_text = f"；平均复盘得分 {sum(scored)/len(scored):.1f}/5" if scored else "；尚无统一评分"
        trend_parts += [f"### I{index} · {item.get('date') or '日期未知'} · {item.get('company') or ''}",
                        f"- 题量：{len(item.get('questions') or [])}{score_text}",
                        f"- 结果：{item.get('actual_result') or '未记录'}", ""]
    trend_parts += ["## 跨场变化", "- **已解决**：需要至少两场同类问题且有评分改善证据，当前按证据判断。",
                    "- **正在改善**：后续生成会结合逐题评分和回答版本比较。",
                    "- **反复出现**：" + ("、".join(f"{topic}（{count}次）" for topic, count in recurring) if recurring else "尚无达到两次的同类考点"),
                    "- **新出现**：只在最近一场出现的主题需要继续观察。", "- **尚未验证**：没有真实结果或评分的问题不作进步判断。"]
    trend = "\n".join(trend_parts)
    preparation = "\n".join((
        f"# 下一次{analysis['role_family']}面试准备计划", "",
        "## 准备原则", "- 先修复跨场反复出现的问题，再补目标岗位新增知识。", "- 每个行动必须指向一道真实题或一份可编辑文档。", "",
        "## 优先行动",
        *([f"- [ ] **补强 {topic}**：回看出现过的 {count} 道真题，重写答案并完成一次追问模拟。" for topic, count in recurring[:5]] or ["- [ ] 完成已有真题的逐题复盘，建立第一批可比较基线。"]), "",
        "## 目标岗位增量", analysis.get("target_jd") or "尚未提供目标 JD；补充后再生成岗位特定增量。", "",
        "## 完成标准", "- 能在 90 秒内给出结论、依据和具体例子。", "- 对高频主题至少准备一个真实经历证据和两个连续追问。",
    ))
    selected = []
    seen = set()
    for round_item, question in reversed(all_questions):
        text = str(question.get("question") or "").strip()
        fingerprint = re.sub(r"\s+", "", text)
        if not text or fingerprint in seen:
            continue
        seen.add(fingerprint); selected.append((round_item, question))
        if len(selected) >= mock_count:
            break
    mock_lines = [f"# {analysis['role_family']}模拟面试卷", "", "## 使用说明", "按顺序作答；每题完成后再查看参考答案。题目优先来自真实历史面试。", ""]
    answer_lines = [f"# {analysis['role_family']}模拟答卷", "", "> 这是你的可编辑答卷。先独立作答，再对照参考答案。", ""]
    reference_lines = [f"# {analysis['role_family']}模拟面试参考答案", "", "> 参考答案只提供结构与证据方向，不替用户编造经历。", ""]
    for index, (round_item, question) in enumerate(selected, 1):
        text = question.get("question") or ""
        source = f"{round_item.get('company') or ''} · 第 {round_item.get('round_number') or '?'} 轮 · Q{question.get('order') or '?'}"
        mock_lines += [f"## Q{index} · {_growth_topic(question)}", text, f"*来源：{source}*", ""]
        answer_lines += [f"## Q{index} · {text}", "", "在这里填写你的答案。", ""]
        review = question.get("review") or {}
        basis = review.get("better_answer") or review.get("better_approach") or question.get("answer")
        reference_lines += [f"## Q{index} · {text}", "", basis or "暂无可靠历史答案。请先补充真实事实，再生成参考答法。", ""]
    if not selected:
        mock_lines += ["## 暂无题目", "请先选择至少一场已拆解的真实面试。"]
        answer_lines += ["## 暂无题目", "等待生成。"]
        reference_lines += ["## 暂无题目", "等待生成。"]
    return {"PROFILE": profile, "TREND": trend, "PREPARATION": preparation,
            "MOCK": "\n".join(mock_lines), "ANSWER_SHEET": "\n".join(answer_lines),
            "REFERENCE": "\n".join(reference_lines)}


def _parse_growth_sections(text: str) -> dict[str, str]:
    keys = ("PROFILE", "TREND", "PREPARATION", "MOCK", "ANSWER_SHEET", "REFERENCE")
    result = {}
    for index, key in enumerate(keys):
        marker = f"==={key}==="
        if marker not in text:
            continue
        body = text.split(marker, 1)[1]
        next_positions = [body.find(f"==={candidate}===") for candidate in keys[index + 1:]]
        next_positions = [position for position in next_positions if position >= 0]
        result[key] = body[:min(next_positions) if next_positions else None].strip()
    return result


def _growth_model_sections(analysis: dict, snapshot: dict, fallback: dict[str, str], mock_count: int) -> dict[str, str]:
    evidence = json.dumps(snapshot, ensure_ascii=False)
    prompt = f"""你是面试研究分析师。请基于下面冻结的真实证据，为同一岗位类别生成跨场成长分析和下一轮训练材料。

岗位类别：{analysis['role_family']}
细分方向：{analysis.get('role_subtype') or '未指定'}
目标 JD：{analysis.get('target_jd') or '未提供'}
模拟题数量：{mock_count}

硬约束：
1. 事实、推断、未知必须分开；不能补写用户没有说过的经历或结果。
2. “反复问题”至少在两场面试出现；“进步”必须有时间顺序和回答/评分证据。
3. 引用证据时使用 I1/Q1 这样的索引，便于回看原始面试。
4. 不要返回 JSON。严格按六个分隔符输出完整 Markdown 正文。
5. 模拟卷优先复用真实题并结合目标 JD；答卷是空白可编辑模板；参考答案只给结构、历史可靠答案和待补事实。

输出：
===PROFILE===
能力画像：证据范围、高频考点、稳定优势、反复短板、未验证项。
===TREND===
逐场时间线，以及已解决/正在改善/反复出现/新出现/未验证五类变化。
===PREPARATION===
下一轮准备计划，每一项要有来源题、具体动作、完成标准。
===MOCK===
完整结构化模拟试卷。
===ANSWER_SHEET===
与试卷逐题对应的可编辑空白答卷。
===REFERENCE===
逐题参考结构、可用历史证据、不可编造的事实缺口和可能追问。

冻结证据：
{evidence}
"""
    try:
        response = ai.chat(
            [{"role": "user", "content": prompt}],
            system="只依据冻结面试证据进行跨场比较。不得把推测写成事实。",
            max_tokens=9000, timeout=240, profile_key="deep_reasoning",
        )
        parsed = _parse_growth_sections(response or "")
        if all(len(parsed.get(key, "")) >= 80 for key in fallback):
            return parsed
    except Exception:
        pass
    return fallback


def _upsert_growth_document(analysis: dict, name: str, title: str, document_type: str,
                            body: str) -> int:
    field = f"{name}_document_id"
    did = analysis.get(field)
    payload = {"title": title, "body": body, "change_summary": "更新跨场面试分析", "created_by": "ai"}
    if did and interview_store.get_document(did):
        document, _ = interview_store.update_document(did, payload)
        return document["id"]
    return interview_store.create_document({
        **payload, "document_type": document_type, "scope_type": "track" if analysis.get("target_track_id") else "global",
        "track_id": analysis.get("target_track_id"), "folder_key": "interview_growth",
        "source_type": "agent", "source_ref_type": "interview_growth_analysis",
        "source_ref_id": analysis["id"], "editable": True,
    })


def _run_interview_growth_task(task_id: int, analysis_id: int, mock_count: int = 10):
    run_id = None
    try:
        run_id = _retry_sqlite_operation(lambda: db.create_agent_run({
            "task_id": task_id, "run_type": "primary", "expert_key": "review_analyst",
            "model_profile": "deep_reasoning", "status": "running",
        }))
        _retry_sqlite_operation(lambda: db.update_agent_task(task_id, status="active"))
        analysis = interview_store.get_growth_analysis(analysis_id)
        if not analysis:
            raise RuntimeError("跨场分析不存在")
        snapshot = _build_growth_snapshot(analysis.get("selected_round_ids") or [])
        if not snapshot.get("rounds"):
            raise RuntimeError("没有可分析的已拆解面试")
        interview_store.update_growth_analysis(analysis_id, {
            "status": "running", "progress": 22, "stage": "已冻结历史面试证据", "source_snapshot": snapshot,
        })
        db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "growth_scope_ready",
                               "label": f"已冻结 {len(snapshot['rounds'])} 场历史面试", "status": "running"})
        fallback = _growth_fallback_sections(analysis, snapshot, mock_count)
        interview_store.update_growth_analysis(analysis_id, {
            "status": "running", "progress": 46, "stage": "正在比较时间趋势与反复问题",
        })
        db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "growth_profile",
                               "label": "正在建立跨场能力画像", "status": "running"})
        sections = _growth_model_sections(analysis, snapshot, fallback, mock_count)
        interview_store.update_growth_analysis(analysis_id, {
            "status": "running", "progress": 72, "stage": "正在生成下一轮准备与模拟训练",
        })
        db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "growth_trend",
                               "label": "已完成时间趋势与反复问题识别", "status": "running"})
        titles = {
            "profile": (f"{analysis['role_family']}面试能力画像", "growth_profile", "PROFILE"),
            "trend": (f"{analysis['role_family']}面试时间趋势", "growth_trend", "TREND"),
            "preparation": (f"下一次{analysis['role_family']}面试准备计划", "growth_preparation", "PREPARATION"),
            "mock": (f"{analysis['role_family']}模拟面试卷", "mock_exam", "MOCK"),
            "answer_sheet": (f"{analysis['role_family']}模拟答卷", "mock_answer_sheet", "ANSWER_SHEET"),
            "reference": (f"{analysis['role_family']}模拟面试参考答案", "mock_reference", "REFERENCE"),
        }
        updates = {}
        interview_store.update_growth_analysis(analysis_id, {
            "status": "running", "progress": 86, "stage": "正在保存六份可编辑文档",
        })
        for name, (title, document_type, section_key) in titles.items():
            updates[f"{name}_document_id"] = _upsert_growth_document(
                analysis, name, title, document_type, sections[section_key],
            )
        db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "growth_training",
                               "label": "已生成下一轮准备和模拟训练", "status": "running"})
        summary = f"汇总 {len(snapshot['rounds'])} 场面试、{sum(len(item.get('questions') or []) for item in snapshot['rounds'])} 道真实题"
        interview_store.update_growth_analysis(analysis_id, {
            **updates, "status": "completed", "progress": 100, "stage": "分析与训练材料已完成",
            "summary": summary, "error_message": None,
        })
        db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "growth_documents_saved",
                               "label": "六份可编辑文档已保存", "status": "running"})
        db.update_agent_run(run_id, status="completed", summary=summary,
                            output_json=json.dumps({"analysis_id": analysis_id, **updates}, ensure_ascii=False))
        db.update_agent_task(task_id, status="completed", result_summary=summary)
        db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "completed",
                               "label": "综合分析与训练已完成", "detail": summary, "status": "done"})
        _commit("完成跨场面试综合分析与训练")
    except Exception as exc:
        detail = str(exc)[:300]
        if run_id:
            try: db.update_agent_run(run_id, status="failed", summary=detail)
            except Exception: pass
        try: db.update_agent_task(task_id, status="failed", result_summary=detail)
        except Exception: pass
        try: interview_store.update_growth_analysis(analysis_id, {"status": "failed", "stage": "生成失败", "error_message": detail})
        except Exception: pass
        try: db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "failed",
                                    "label": "综合分析生成失败", "detail": detail, "status": "failed"})
        except Exception: pass
    finally:
        with _INTERVIEW_WORKERS_LOCK:
            _INTERVIEW_WORKERS.discard(task_id)


def _schedule_interview_growth(task_id: int, analysis_id: int, mock_count: int = 10):
    with _INTERVIEW_WORKERS_LOCK:
        if task_id in _INTERVIEW_WORKERS:
            return
        _INTERVIEW_WORKERS.add(task_id)
    threading.Thread(target=_run_interview_growth_task, args=(task_id, analysis_id, mock_count),
                     name=f"interview-growth-{task_id}", daemon=True).start()


def _recover_interview_growth_tasks():
    if not product_features.get_feature("interview_growth_analysis").get("allow_task_creation"):
        return
    for task in db.list_agent_tasks_for_recovery("interview_growth_analysis"):
        analysis_id = int(task.get("object_id") or 0)
        analysis = interview_store.get_growth_analysis(analysis_id) if analysis_id else None
        if not analysis:
            continue
        context_data = {}
        try: context_data = json.loads(task.get("context_json") or "{}")
        except (TypeError, json.JSONDecodeError): pass
        db.update_agent_task(task["id"], status="queued", result_summary="服务重启，正在自动续跑")
        db.create_agent_event({"task_id": task["id"], "event_type": "recovered",
                               "label": "服务重启后自动续跑综合分析", "status": "running"})
        _schedule_interview_growth(task["id"], analysis_id, int(context_data.get("mock_question_count") or 10))


@app.get("/api/v2/interview-growth/scope")
def interview_growth_scope_v2(role_family: str, role_subtype: Optional[str] = None,
                              date_from: Optional[str] = None, date_to: Optional[str] = None):
    _require_feature_available("interview_growth_analysis")
    return {"ok": True, "data": {"candidates": _growth_scope_candidates(
        role_family, role_subtype, date_from, date_to,
    )}}


@app.get("/api/v2/interview-growth-analyses")
def list_interview_growth_analyses_v2(limit: int = 50):
    return {"ok": True, "items": interview_store.list_growth_analyses(max(1, min(limit, 100)))}


@app.post("/api/v2/interview-growth-analyses")
def create_interview_growth_analysis_v2(body: InterviewGrowthAnalysisIn):
    _require_feature_available("interview_growth_analysis")
    round_ids = list(dict.fromkeys(int(item) for item in body.selected_round_ids if int(item) > 0))
    if not round_ids:
        round_ids = [item["id"] for item in _growth_scope_candidates(
            body.role_family, body.role_subtype, body.date_from, body.date_to,
        )]
    if not round_ids:
        raise HTTPException(400, "没有匹配到已拆解的历史面试，请先选择面试轮次")
    valid_ids = [rid for rid in round_ids if interview_store.get_round(rid) and interview_store.get_exam(rid)]
    if not valid_ids:
        raise HTTPException(400, "所选面试还没有结构化答卷")
    title = (body.title or f"{body.role_family}综合分析与训练").strip()
    analysis_id = interview_store.create_growth_analysis({
        **body.dict(), "title": title, "selected_round_ids": valid_ids,
        "status": "queued", "stage": "等待冻结历史面试证据",
    })
    task_id = db.create_agent_task({
        "task_type": "interview_growth_analysis", "title": title,
        "instruction": "汇总真实面试，分析时间变化并生成下一轮模拟训练",
        "object_type": "interview_growth_analysis", "object_id": analysis_id,
        "track_id": body.target_track_id, "status": "queued", "assigned_expert": "review_analyst",
        "context_json": json.dumps({"mock_question_count": body.mock_question_count}, ensure_ascii=False),
    })
    db.create_agent_event({"task_id": task_id, "event_type": "queued", "label": "综合分析已进入队列",
                           "detail": "可以离开页面，完成后会通知并保留六份可编辑文档", "status": "pending"})
    _schedule_interview_growth(task_id, analysis_id, body.mock_question_count)
    _commit("开始跨场面试综合分析")
    return {"ok": True, "data": {"analysis_id": analysis_id, "task_id": task_id, "status": "queued"}}


@app.get("/api/v2/interview-growth-analyses/{analysis_id}")
def get_interview_growth_analysis_v2(analysis_id: int):
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    tasks = db.list_agent_tasks(object_type="interview_growth_analysis", object_id=analysis_id, limit=10)
    return {"ok": True, "data": {**analysis, "tasks": [_interview_task_progress(db.get_agent_task(item["id"]) or item) for item in tasks]}}


@app.post("/api/v2/interview-growth-analyses/{analysis_id}/generate")
def regenerate_interview_growth_analysis_v2(analysis_id: int, body: dict):
    _require_feature_available("interview_growth_analysis")
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    mock_count = max(5, min(int(body.get("mock_question_count") or 10), 30))
    task_id = db.create_agent_task({
        "task_type": "interview_growth_analysis", "title": f"更新：{analysis['title']}",
        "instruction": "基于最新确认范围重新生成跨场分析与训练文档",
        "object_type": "interview_growth_analysis", "object_id": analysis_id,
        "track_id": analysis.get("target_track_id"), "status": "queued", "assigned_expert": "review_analyst",
        "context_json": json.dumps({"mock_question_count": mock_count}, ensure_ascii=False),
    })
    interview_store.update_growth_analysis(analysis_id, {"status": "queued", "progress": 0, "stage": "等待重新生成"})
    db.create_agent_event({"task_id": task_id, "event_type": "queued", "label": "更新任务已进入队列", "status": "pending"})
    _schedule_interview_growth(task_id, analysis_id, mock_count)
    return {"ok": True, "data": {"task_id": task_id, "analysis_id": analysis_id}}


def _growth_mock_payload(analysis_id: int):
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    session = interview_store.get_growth_mock_session(analysis_id)
    messages = []
    voice_turns = interview_store.list_growth_mock_voice_turns(analysis_id)
    voices_by_message = {
        int(item["message_id"]): item for item in voice_turns if item.get("message_id")
    }
    if session:
        messages = [
            {
                "id": item.get("id"), "role": item.get("role"),
                "content": item.get("content") or "",
                "voice": voices_by_message.get(int(item.get("id") or 0)),
            }
            for item in db.get_chat_history(session["session_key"], limit=300)
            if item.get("role") in {"user", "assistant"}
        ]
    ready_voices = [item for item in voice_turns if item.get("status") == "ready"]
    duration_ms = sum(int(item.get("duration_ms") or 0) for item in ready_voices)
    filler_total = sum(int((item.get("metrics") or {}).get("filler_total") or 0) for item in ready_voices)
    return {
        "analysis_id": analysis_id,
        "session": session,
        "status": session.get("status") if session else "not_started",
        "messages": messages,
        "answer_count": sum(1 for item in messages if item["role"] == "user"),
        "question_target": int((session or {}).get("question_target") or 10),
        "answer_sheet_document_id": analysis.get("answer_sheet_document_id"),
        "voice_turns": voice_turns,
        "voice_summary": {
            "answer_count": len(ready_voices), "duration_ms": duration_ms,
            "filler_total": filler_total,
        },
    }


def _growth_mock_system(analysis: dict, question_target: int):
    documents = analysis.get("documents") or {}

    def document_body(key: str, limit: int):
        return str((documents.get(key) or {}).get("body") or "")[:limit]

    snapshot = analysis.get("source_snapshot") or {}
    return f"""你是一位严格、自然、会连续追问的真实面试官。你正在对候选人进行模拟面试，而不是提供面试辅导。

【目标岗位类别】{analysis.get('role_family') or '通用岗位'}
【细分方向】{analysis.get('role_subtype') or '未限定'}
【下一场岗位 / JD】
{(analysis.get('target_jd') or '')[:5000]}

【历史面试能力画像】
{document_body('profile', 5000)}

【下一轮准备重点】
{document_body('preparation', 5000)}

【隐藏出题计划】
{document_body('mock', 8000)}

【纳入分析的历史面试数】{len(snapshot.get('rounds') or analysis.get('selected_round_ids') or [])}

规则：
1. 一次只问一个问题，问完必须停下等待候选人回答。
2. 总体目标约 {question_target} 道主问题。要根据候选人的回答继续追问事实、个人贡献、数据、取舍和反思，不要机械照抄题单。
3. 模拟过程中不要给参考答案、评分、批改建议或教学提示，也不要暴露隐藏出题计划。
4. 保持真实面试节奏：可以做简短承接，但不要每题都表扬，也不要自问自答。
5. 优先覆盖历史面试反复暴露的薄弱点，并结合目标 JD；不得编造候选人的经历和事实。
6. 当主问题与追问已经足够时，可以提示候选人本轮问题已覆盖，但仍等待用户主动结束模拟。
7. 全程使用中文。首次发言只需一句简短开场，然后提出第一个问题。"""


def _fallback_growth_mock_answer_sheet(analysis: dict, messages: list[dict]):
    lines = [
        f"# {analysis.get('role_family') or '岗位'}模拟面试答卷",
        "",
        "> 本文档由本次模拟面试对话整理生成。未补充或未经验证的事实不会被系统代写。",
        "",
        "## 一、模拟记录",
        "",
    ]
    question_number = 0
    for item in messages:
        if item.get("role") == "assistant":
            question_number += 1
            lines.extend([f"### Q{question_number} 面试官", "", item.get("content") or "", ""])
        elif item.get("role") == "user":
            lines.extend(["**我的回答**", "", item.get("content") or "", ""])
    lines.extend([
        "## 二、待复盘",
        "",
        "- 本次对话已完整留存，请结合目标岗位逐题检查事实、证据、表达结构和追问承接。",
    ])
    return "\n".join(lines)


@app.get("/api/v2/interview-growth-analyses/{analysis_id}/mock")
def get_interview_growth_mock_v2(analysis_id: int):
    return {"ok": True, "data": _growth_mock_payload(analysis_id)}


@app.post("/api/v2/interview-growth-analyses/{analysis_id}/mock/voice")
async def transcribe_interview_growth_mock_voice_v2(
    analysis_id: int,
    file: UploadFile = File(...),
    duration_ms: int = Form(0),
):
    _require_feature_available("interview_growth_analysis")
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    session = interview_store.get_growth_mock_session(analysis_id)
    if not session or session.get("status") != "active":
        raise HTTPException(409, "本轮模拟尚未开始或已经结束")
    duration_ms = max(0, int(duration_ms or 0))
    if duration_ms > 31_000:
        raise HTTPException(400, "当前单次语音最多录制 30 秒，请分段回答")
    content = await file.read()
    if not content:
        raise HTTPException(400, "没有收到有效音频")
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(400, "单段音频不能超过 25MB")
    mime_type = (file.content_type or "audio/wav").split(";", 1)[0]
    suffix = Path(file.filename or "answer.wav").suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a", ".webm", ".ogg"}:
        suffix = ".wav"
    store_dir = ai.CONFIG_DIR / "interview_audio" / str(analysis_id)
    store_dir.mkdir(parents=True, exist_ok=True)
    audio_path = store_dir / f"{uuid.uuid4().hex}{suffix}"
    audio_path.write_bytes(content)
    voice_id = interview_store.create_growth_mock_voice_turn({
        "analysis_id": analysis_id,
        "session_key": session["session_key"],
        "audio_path": str(audio_path),
        "mime_type": mime_type,
        "duration_ms": duration_ms,
        "status": "transcribing",
    })
    try:
        result = await run_in_threadpool(
            speech.transcribe_audio,
            audio_path,
            mime_type,
            f"{analysis.get('role_family') or '求职'}模拟面试回答，请准确保留专业名词与项目名称。",
        )
        raw = result["text"]
        cleaned = speech.clean_transcript(raw)
        metrics = speech.analyze_delivery(raw, duration_ms)
        voice_turn = interview_store.update_growth_mock_voice_turn(voice_id, {
            "transcript_raw": raw,
            "transcript_edited": cleaned,
            "metrics": metrics,
            "provider": result.get("provider"),
            "model": result.get("model"),
            "status": "ready",
            "error_message": None,
        })
    except speech.SpeechError as exc:
        interview_store.update_growth_mock_voice_turn(voice_id, {
            "status": "failed", "error_message": str(exc),
        })
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": voice_turn}


@app.post("/api/v2/interview-growth-analyses/{analysis_id}/mock/start")
def start_interview_growth_mock_v2(analysis_id: int, reset: bool = False):
    _require_feature_available("interview_growth_analysis")
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    question_target = max(5, min(int(analysis.get("mock_question_count") or 10), 30))
    if reset:
        old_session = interview_store.get_growth_mock_session(analysis_id)
        for audio_path in interview_store.clear_growth_mock_voice_turns(analysis_id):
            try:
                Path(audio_path).unlink(missing_ok=True)
            except OSError:
                pass
        if old_session:
            db.clear_chat(old_session["session_key"])
    session = interview_store.start_growth_mock_session(analysis_id, question_target, reset=reset)
    existing = db.get_chat_history(session["session_key"], limit=300)
    if existing:
        return {"ok": True, "data": _growth_mock_payload(analysis_id)}
    try:
        reply = ai.chat(
            [{"role": "user", "content": "请开始本轮模拟面试。"}],
            system=_growth_mock_system(analysis, question_target),
            max_tokens=1200,
            profile_key="deep_reasoning",
        )
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"模拟面试启动失败：{str(exc)[:200]}")
    db.save_message(session["session_key"], "assistant", reply)
    return {"ok": True, "data": _growth_mock_payload(analysis_id)}


@app.post("/api/v2/interview-growth-analyses/{analysis_id}/mock/respond")
def respond_interview_growth_mock_v2(analysis_id: int, body: InterviewGrowthMockMessageIn):
    _require_feature_available("interview_growth_analysis")
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    session = interview_store.get_growth_mock_session(analysis_id)
    if not session or session.get("status") != "active":
        raise HTTPException(409, "本轮模拟尚未开始或已经结束")
    history = db.get_chat_history(session["session_key"], limit=300)
    messages = [
        {"role": item["role"], "content": item.get("content") or ""}
        for item in history if item.get("role") in {"user", "assistant"}
    ]
    messages.append({"role": "user", "content": body.message.strip()})
    try:
        reply = ai.chat(
            messages,
            system=_growth_mock_system(analysis, int(session.get("question_target") or 10)),
            max_tokens=1400,
            profile_key="deep_reasoning",
        )
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"模拟面试继续失败：{str(exc)[:200]}")
    message_id = db.save_message(session["session_key"], "user", body.message.strip())
    if body.voice_turn_id:
        voice_turn = interview_store.get_growth_mock_voice_turn(body.voice_turn_id)
        if (
            voice_turn and int(voice_turn.get("analysis_id") or 0) == analysis_id
            and voice_turn.get("session_key") == session["session_key"]
            and voice_turn.get("status") == "ready"
        ):
            interview_store.update_growth_mock_voice_turn(body.voice_turn_id, {
                "message_id": message_id,
                "transcript_edited": body.message.strip(),
            })
    db.save_message(session["session_key"], "assistant", reply)
    return {"ok": True, "data": _growth_mock_payload(analysis_id)}


def _growth_delivery_report_markdown(history: list[dict], voice_turns: list[dict]) -> str:
    answers = [str(item.get("content") or "").strip() for item in history if item.get("role") == "user"]
    answer_metrics = [speech.analyze_delivery(answer) for answer in answers]
    combined = speech.analyze_delivery("\n".join(answers))
    duration_ms = sum(int(item.get("duration_ms") or 0) for item in voice_turns)
    timed_metrics = speech.analyze_delivery("\n".join(answers), duration_ms) if duration_ms else None

    top_fillers = "、".join(
        f"{item['term']}（{item['count']}次）" for item in combined.get("top_fillers") or []
    ) or "未发现明显高频口头词"
    structure_markers = "、".join(
        f"{item['term']}（{item['count']}次）" for item in combined.get("structure_markers") or []
    ) or "较少使用显式结构词"
    risky_answers = sorted(
        enumerate(answer_metrics, start=1),
        key=lambda pair: (
            pair[1].get("filler_total", 0) * 8
            + pair[1].get("long_sentence_count", 0) * 6
            + max(0, pair[1].get("char_count", 0) - 350) / 30
            - pair[1].get("structure_marker_total", 0)
        ),
        reverse=True,
    )[:3]
    priority_lines = []
    for index, metrics in risky_answers:
        reasons = []
        if metrics.get("filler_total"):
            reasons.append(f"口头词 {metrics['filler_total']} 次")
        if metrics.get("long_sentence_count"):
            reasons.append(f"长句 {metrics['long_sentence_count']} 处")
        if metrics.get("verbosity_level") in {"较长", "偏长"}:
            reasons.append(f"回答{metrics['verbosity_level']}")
        if metrics.get("char_count", 0) >= 120 and metrics.get("structure_marker_total", 0) < 2:
            reasons.append("结构标记不足")
        priority_lines.append(f"- 第 {index} 个回答：{'；'.join(reasons) if reasons else '表达相对稳定，可继续压缩开场结论'}")

    suggestions = []
    for metrics in answer_metrics:
        for suggestion in metrics.get("suggestions") or []:
            if suggestion not in suggestions:
                suggestions.append(suggestion)

    lines = [
        "## 五、表达批改报告",
        "",
        "### 数据摘要",
        "",
        f"- 已评估回答：{len(answers)} 个，共 {combined.get('char_count', 0)} 个有效字",
        f"- 语气词与冗余衔接：{combined.get('filler_total', 0)} 次，约占有效字数 {combined.get('filler_density', 0)}%",
        f"- 高频表达：{top_fillers}",
        f"- 结构标记：{structure_markers}",
        f"- 长句：{combined.get('long_sentence_count', 0)} 处，最长约 {combined.get('max_sentence_chars', 0)} 字",
    ]
    if timed_metrics and timed_metrics.get("chars_per_minute"):
        lines.append(f"- 有录音时长证据的整体语速：约 {timed_metrics['chars_per_minute']} 字/分钟（{timed_metrics['pace']}）")
    else:
        lines.append("- 真实语速：未知（本轮仅有文字证据）")
    lines.extend([
        "",
        "### 主要表达习惯",
        "",
        f"- 冗余程度：{combined.get('verbosity_level', '未知')}",
        f"- 可观察问题：{'；'.join(suggestions[:4]) if suggestions else '未发现明显文字层表达问题'}",
        "",
        "### 需要优先修改的回答",
        "",
        *(priority_lines or ["- 暂无可评估回答"]),
        "",
        "### 下一轮训练规则",
        "",
        *[f"- {item}" for item in suggestions[:4]],
        "",
        f"> 证据边界：{combined.get('limitation')}",
    ])
    return "\n".join(lines)


@app.post("/api/v2/interview-growth-analyses/{analysis_id}/mock/finish")
def finish_interview_growth_mock_v2(analysis_id: int):
    _require_feature_available("interview_growth_analysis")
    analysis = interview_store.get_growth_analysis(analysis_id)
    if not analysis:
        raise HTTPException(404, "综合分析不存在")
    session = interview_store.get_growth_mock_session(analysis_id)
    if not session:
        raise HTTPException(409, "本轮模拟尚未开始")
    history = [
        {"role": item["role"], "content": item.get("content") or ""}
        for item in db.get_chat_history(session["session_key"], limit=300)
        if item.get("role") in {"user", "assistant"}
    ]
    if not any(item["role"] == "user" for item in history):
        raise HTTPException(400, "至少回答一道问题后才能生成模拟答卷")
    transcript = "\n\n".join(
        f"{'面试官' if item['role'] == 'assistant' else '候选人'}：{item['content']}"
        for item in history
    )
    prompt = f"""请把下面的模拟面试对话整理为一份结构完整、可继续编辑的 Markdown 模拟答卷。

必须包含：
# 标题
## 一、本轮整体表现（只根据对话证据判断）
## 二、逐题答卷（每题包含：面试官问题、我的真实回答、追问链、回答诊断、优化思路、参考答法）
## 三、反复暴露的薄弱点
## 四、下一轮训练清单

约束：
- 保留候选人的真实事实，不得虚构项目、数据、职责或面试反馈。
- 可以去掉语气词、重复表达并整理完整语义，但必须区分真实回答与建议答法。
- 如果证据不足，明确写“待补事实”，不要代替候选人编造。
- 输出正文，不要使用 JSON，不要添加代码围栏。

【模拟对话】
{transcript[:100000]}"""
    try:
        body = ai.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=6000,
            profile_key="deep_reasoning",
        )
    except Exception:
        body = _fallback_growth_mock_answer_sheet(analysis, history)
    voice_turns = [
        item for item in interview_store.list_growth_mock_voice_turns(analysis_id)
        if item.get("status") == "ready" and item.get("message_id")
    ]
    body = body.rstrip() + "\n\n" + _growth_delivery_report_markdown(history, voice_turns) + "\n"
    document_id = analysis.get("answer_sheet_document_id")
    if not document_id:
        raise HTTPException(409, "模拟答卷文档尚未生成，请先完成综合分析")
    updated, error = interview_store.update_document(document_id, {
        "title": f"{analysis.get('role_family') or '岗位'}模拟面试答卷",
        "body": body,
        "change_summary": "结束对话式模拟面试并归档答卷",
        "created_by": "interview_mock",
    })
    if error:
        raise HTTPException(409, error)
    interview_store.update_growth_mock_session(analysis_id, {
        "status": "completed", "ended_at": datetime.now().isoformat(timespec="seconds"),
    })
    _commit("完成对话式模拟面试并归档答卷")
    return {"ok": True, "data": {**_growth_mock_payload(analysis_id), "document": updated}}


@app.get("/api/v2/interview-library")
def interview_library_v2(q: Optional[str] = None, track_id: Optional[int] = None):
    """Cross-role read model.  Preparation and editing stay in the job track."""
    return {"ok": True, "data": interview_store.interview_library(q, track_id)}


@app.put("/api/v2/interview-rounds/{rid}/outcome")
def save_interview_outcome_v2(rid: int, body: InterviewOutcomeV2In):
    if body.actual_result not in {'passed','failed','pending','withdrawn','cancelled','unknown'}: raise HTTPException(400,'不支持的面试结果')
    if not interview_store.get_round(rid): raise HTTPException(404,'面试轮次不存在')
    prediction=interview_store.latest_prediction(rid) or {}; hit={'likely_pass':'passed','likely_fail':'failed'}.get(prediction.get('label'))==body.actual_result
    note=("预测与实际结果一致。" if hit else "预测与实际结果不一致；请结合 HR 反馈判断是证据不足、岗位偏好还是模型判断偏差。") if body.actual_result in {'passed','failed'} else '结果尚未确定，暂不校准。'
    interview_store.set_outcome(rid,{**body.dict(),'calibration_note':note});interview_store.update_round(rid,{"status":"completed" if body.actual_result in {'passed','failed','withdrawn','cancelled'} else 'awaiting_result'})
    _commit('记录面试实际结果并校准预测');return {"ok":True,"data":{**(interview_store.get_outcome(rid) or {}),"prediction_hit":hit if body.actual_result in {'passed','failed'} else None}}


class KnowledgeSpaceAgentIn(BaseModel):
    instruction: str
    current_document_id: Optional[int] = None


class KnowledgeSpaceAgentApplyIn(BaseModel):
    change_ids: list[int]


class AgentTaskIn(BaseModel):
    task_type: str = "general"
    title: str
    instruction: str
    object_type: Optional[str] = None
    object_id: Optional[int] = None
    track_id: Optional[int] = None
    company: Optional[str] = None
    priority: str = "normal"
    assigned_expert: Optional[str] = None
    conversation_id: Optional[str] = None
    context: Optional[dict] = None


class WorkspaceSessionResolveIn(BaseModel):
    workspace_task_key: str
    mode: str
    title: str = "Agent 工作台"
    track_id: Optional[int] = None
    knowledge_item_id: Optional[int] = None
    gap_id: Optional[int] = None
    experience_id: Optional[int] = None
    experience_type: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    project_id: Optional[int] = None


class ExperienceDispatchIn(BaseModel):
    message: str
    requested_expert: Optional[str] = None


class ExperienceCandidateIn(BaseModel):
    target_id: int
    field: str
    proposed_value: str
    original_value: Optional[str] = None
    evidence: list[dict] = Field(default_factory=list)
    evidence_source: Literal["material", "user_statement", "wording_only"] = "wording_only"
    unverified_claims: list[str] = Field(default_factory=list)
    question_plan: list[dict] = Field(default_factory=list)


class ExperienceCandidateApplyIn(BaseModel):
    candidate_ids: list[int]


class ExperienceCandidateReviseIn(BaseModel):
    proposed_value: str
    evidence: list[dict] = Field(default_factory=list)
    evidence_source: Literal["material", "user_statement", "wording_only"] = "wording_only"
    unverified_claims: list[str] = Field(default_factory=list)


class WorkspaceMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class KnowledgeDispatchIn(StrictInputModel):
    message: str = Field(..., min_length=1, max_length=50_000)


class KnowledgeDispatchRetryIn(StrictInputModel):
    failure_task_id: int


class AgentCoordinateIn(BaseModel):
    instruction: str
    title: Optional[str] = None
    object_type: Optional[str] = None
    object_id: Optional[int] = None
    track_id: Optional[int] = None
    company: Optional[str] = None
    conversation_id: Optional[str] = None
    priority: str = "normal"
    create_task: bool = False


class AgentTaskApplyIn(BaseModel):
    change_ids: list[int]


class AgentTaskRetryFailedIn(BaseModel):
    change_ids: list[int] = Field(default_factory=list)


class AgentProposalEditIn(BaseModel):
    proposed_title: Optional[str] = None
    proposed_content: Optional[str] = None
    reason: Optional[str] = None


class AgentProposalRejectIn(BaseModel):
    reason: str = ""


class AgentIntegrationInstallIn(BaseModel):
    confirmed: bool = False


class AgentMigrationStartIn(BaseModel):
    agent_key: str = "external_agent"
    roots: list[str] = Field(default_factory=list)
    include_types: list[str] = Field(default_factory=lambda: [
        "个人资料", "简历版本", "实习经历", "项目经历", "岗位与 JD", "面试资料",
    ])
    notes: str = ""


class AgentWorkspaceBootstrapIn(BaseModel):
    agent_key: str = "external_agent"
    task_hint: Optional[str] = None
    track_id: Optional[int] = None
    project_id: Optional[int] = None


class AgentContextPackageIn(BaseModel):
    track_id: Optional[int] = None
    project_id: Optional[int] = None
    interview_round_id: Optional[int] = None
    document_type: Optional[str] = None
    document_id: Optional[int] = None
    intent: str = "external_agent"
    query: Optional[str] = None
    detail: str = "brief"


class ExternalAgentRunIn(BaseModel):
    title: str
    instruction: str
    agent_key: str = "external_agent"
    track_id: Optional[int] = None
    object_type: Optional[str] = None
    object_id: Optional[int] = None
    context: Optional[dict] = None


class ExternalAgentEventIn(BaseModel):
    label: str
    detail: str = ""
    status: str = "done"
    payload: Optional[dict] = None


class ExternalDraftAssetIn(BaseModel):
    title: str
    body: str
    asset_type: str = "other"
    track_id: Optional[int] = None
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    agent_key: str = "external_agent"
    provenance: Optional[dict] = None


class ExternalFactProposalIn(BaseModel):
    task_id: int
    subject_type: str
    predicate: str
    value_text: str
    subject_id: Optional[int] = None
    scope_type: str = "global"
    scope_id: Optional[int] = None
    confidence: Optional[float] = None
    evidence: list[dict] = Field(default_factory=list)
    reason: str = ""
    agent_key: str = "external_agent"


class ExternalFeedbackProposalIn(BaseModel):
    task_id: int
    original_text: str
    scope: str = "global"
    scope_id: Optional[int] = None
    category: Optional[str] = None
    polarity: Optional[str] = None
    strength: Optional[str] = None
    directive: Optional[str] = None
    reason: str = ""
    agent_key: str = "external_agent"


class ExternalTrackKnowledgeProposalIn(BaseModel):
    task_id: int
    track_id: int
    title: str
    body: str
    folder_key: str = "interview"
    topic: str = ""
    mastery: str = "learning"
    reason: str = ""
    evidence: list[dict] = Field(default_factory=list)
    agent_key: str = "external_agent"


# ─── Alpha 首次体验、反馈与本地使用分析 ─────────────────────────────────────

def _app_setting(key: str, default: str = "") -> str:
    conn = db.get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def _set_app_setting(key: str, value: str):
    conn = db.get_db()
    conn.execute(
        """INSERT INTO app_settings(key,value,updated_at)
           VALUES(?,?,datetime('now','localtime'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, value),
    )
    conn.commit()
    conn.close()


@app.get("/api/user-profile")
def get_user_profile():
    raw = _app_setting("user_profile", "{}")
    try:
        profile = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        profile = {}
    for education in profile.get("education") or []:
        if not isinstance(education, dict):
            continue
        legacy_fields = {
            "coursework": ("courses", "name"),
            "honors": ("honors", "name"),
            "activities": ("activities", "organization"),
        }
        for legacy_key, (target_key, value_key) in legacy_fields.items():
            legacy_value = education.get(legacy_key)
            if legacy_value and not isinstance(education.get(target_key), list):
                education[target_key] = [
                    {value_key: item.strip()}
                    for item in re.split(r"[,，;；\n]+", str(legacy_value))
                    if item.strip()
                ]
        education.setdefault("courses", [])
        education.setdefault("honors", [])
        education.setdefault("activities", [])
    defaults = UserProfileIn().model_dump()
    return {
        "profile": {**defaults, **{k: v for k, v in profile.items() if k in defaults}},
        "account": {
            "mode": "local",
            "signed_in": False,
            "label": "本地资料",
            "cloud_login_available": False,
        },
    }


@app.put("/api/user-profile")
def update_user_profile(body: UserProfileIn):
    profile = body.model_dump()
    for key, value in list(profile.items()):
        if isinstance(value, str):
            profile[key] = value.strip()
    clean_education = []
    for item in profile.get("education") or []:
        clean_item = {}
        for key, value in item.items():
            if isinstance(value, str):
                clean_item[key] = value.strip()
            elif isinstance(value, list):
                clean_item[key] = [
                    {sub_key: str(sub_value or "").strip() for sub_key, sub_value in entry.items()}
                    for entry in value
                    if any(str(sub_value or "").strip() for sub_value in entry.values())
                ]
        if any(value for value in clean_item.values()):
            clean_education.append(clean_item)
    profile["education"] = clean_education
    _set_app_setting("user_profile", json.dumps(profile, ensure_ascii=False))
    return {"ok": True, "profile": profile}


@app.get("/api/onboarding/status")
def onboarding_status():
    conn = db.get_db()
    counts = {
        "sources": conn.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"],
        "tracks": conn.execute("SELECT COUNT(*) n FROM job_tracks").fetchone()["n"],
        "assets": conn.execute("SELECT COUNT(*) n FROM assets").fetchone()["n"],
        "experiences": conn.execute("SELECT COUNT(*) n FROM experiences").fetchone()["n"],
        "projects": conn.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"],
        "knowledge": conn.execute("SELECT COUNT(*) n FROM knowledge_items WHERE status='active'").fetchone()["n"],
        "interviews": conn.execute("SELECT COUNT(*) n FROM interview_rounds").fetchone()["n"],
    }
    conn.close()
    providers = ai.list_providers_masked()
    integrations = _agent_integrations_payload()
    return {
        "completed": _app_setting("onboarding_completed") == "1",
        "product_tour_completed": _app_setting("product_tour_completed") == "1",
        "has_ai": bool(providers.get("active_provider_id")),
        "ai_mode": _app_setting("onboarding_ai_mode") or None,
        "external_agent_ready": any(
            item.get("configured") for item in integrations.get("clients", [])
            if item.get("key") in {"codex", "claude_desktop"}
        ),
        "agent_integrations": integrations,
        "counts": counts,
        "telemetry": telemetry.get_settings(),
        "app_version": telemetry.APP_VERSION,
    }


@app.post("/api/onboarding/complete")
def complete_onboarding(body: OnboardingCompleteIn):
    method = body.import_method if body.import_method in {"upload", "paste", "existing", "skip"} else "skip"
    ai_mode = body.ai_mode if body.ai_mode in {"builtin", "external", "none"} else "none"
    _set_app_setting("onboarding_completed", "1")
    _set_app_setting("onboarding_ai_mode", ai_mode)
    telemetry.log_event(
        "onboarding_completed",
        {
            "import_method": method,
            "ai_mode": ai_mode,
            "ai_configured": bool(ai.get_active_provider()),
        },
    )
    return {"ok": True, "ai_mode": ai_mode}


@app.post("/api/onboarding/tour-complete")
def complete_product_tour():
    _set_app_setting("product_tour_completed", "1")
    telemetry.log_event("product_tour_completed", {"version": "core-loop-v1"})
    return {"ok": True, "product_tour_completed": True}


@app.get("/api/telemetry/settings")
def telemetry_settings():
    return telemetry.get_settings()


@app.put("/api/telemetry/settings")
def update_telemetry_settings(body: TelemetrySettingsIn):
    return telemetry.ensure_required_analytics()


@app.get("/api/telemetry/events")
def telemetry_events(limit: int = 100):
    return {"items": telemetry.list_events(limit)}


@app.delete("/api/telemetry/events")
def clear_telemetry_events():
    return {"ok": True, "deleted": telemetry.clear_events()}


@app.post("/api/telemetry/upload")
def upload_telemetry_events():
    result = telemetry.upload_pending()
    if not result.get("ok"):
        raise HTTPException(502, f"匿名使用数据上传失败：{result.get('error') or '网络错误'}")
    return result


@app.get("/api/telemetry/local-summary")
def telemetry_local_summary(days: int = 30):
    return telemetry.local_summary(days)


@app.post("/api/telemetry/event")
def record_ui_event(body: dict):
    # The registry in telemetry.py rejects unknown events/properties.  The UI
    # endpoint is deliberately limited to view and onboarding start events.
    event_name = body.get("event_name")
    if event_name not in {"view_opened", "onboarding_started"}:
        raise HTTPException(400, "不支持的前端事件")
    try:
        event_id = telemetry.log_event(event_name, body.get("properties") or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "event_id": event_id}


@app.post("/api/product-feedback")
def save_product_feedback(body: ProductFeedbackIn):
    message = body.message.strip()
    if len(message) < 3:
        raise HTTPException(400, "请再具体描述一点")
    if len(message) > 4000:
        raise HTTPException(400, "反馈请控制在 4000 字以内")
    category = body.category if body.category in {"bug", "confusing", "feature", "value", "other"} else "other"
    rating = body.rating if body.rating is None or 1 <= body.rating <= 5 else None
    conn = db.get_db()
    cur = conn.execute(
        """INSERT INTO product_feedback(category,rating,message,contact,app_version)
           VALUES(?,?,?,?,?)""",
        (category, rating, message, (body.contact or "").strip()[:200], telemetry.APP_VERSION),
    )
    conn.commit()
    feedback_id = cur.lastrowid
    conn.close()
    telemetry.log_event(
        "feedback_submitted",
        {"category": category, "rating_bucket": str(rating) if rating else "none"},
        entity_type="product_feedback",
        entity_id=feedback_id,
    )
    return {"ok": True, "id": feedback_id, "storage": "local"}


# ─── 签名更新通道 ────────────────────────────────────────────────────────────

def _update_manifest_url() -> str:
    return (
        os.environ.get("CADDIE_UPDATE_MANIFEST_URL")
        or _app_setting("update_manifest_url")
        or updater.current_state().get("manifest_url")
        or ""
    )


@app.get("/api/update/status")
def update_status():
    value = updater.current_state()
    value["manifest_url"] = _update_manifest_url()
    return value


@app.put("/api/update/channel")
def update_channel(body: UpdateChannelIn):
    url = body.manifest_url.strip()
    if url and not url.startswith("https://"):
        if not (
            os.environ.get("CADDIE_UPDATE_ALLOW_LOCAL") == "1"
            and (url.startswith("file://") or url.startswith("http://127.0.0.1"))
        ):
            raise HTTPException(400, "更新地址必须使用 HTTPS")
    _set_app_setting("update_manifest_url", url)
    return {"ok": True, "manifest_url": url}


@app.post("/api/update/check")
def check_for_update():
    try:
        return updater.check(_update_manifest_url())
    except updater.UpdateError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/update/download")
def download_update():
    try:
        return updater.download()
    except (OSError, requests.RequestException, updater.UpdateError) as exc:
        raise HTTPException(502, f"下载更新失败：{exc}")


@app.post("/api/update/prepare")
def prepare_update():
    try:
        return updater.prepare()
    except updater.UpdateError as exc:
        raise HTTPException(409, str(exc))


def _exit_for_update():
    time.sleep(0.8)
    os._exit(0)


@app.post("/api/update/apply")
def apply_update():
    if not getattr(sys, "frozen", False):
        raise HTTPException(409, "源码运行模式不会自动替换应用")
    try:
        result = updater.launch_apply_helper()
    except updater.UpdateError as exc:
        raise HTTPException(409, str(exc))
    threading.Thread(target=_exit_for_update, daemon=True).start()
    return result


# ─── AI 供应商设置（本 demo 的主角）──────────────────────────────────────────

def _require_ai_settings_unlocked():
    if os.environ.get("CADDIE_LOCK_AI_SETTINGS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        raise HTTPException(403, "公开体验环境已锁定 AI 供应商设置")


@app.get("/api/providers")
def providers():
    return ai.list_providers_masked()


@app.get("/api/settings/providers", deprecated=True)
def providers_legacy():
    """Compatibility alias for the first Alpha frontend contract."""
    return providers()


@app.post("/api/providers")
def save_provider(body: ProviderIn):
    _require_ai_settings_unlocked()
    return ai.upsert_provider(body.dict())


@app.delete("/api/providers/{pid}")
def remove_provider(pid: str):
    _require_ai_settings_unlocked()
    ai.delete_provider(pid)
    return {"ok": True}


@app.post("/api/providers/{pid}/activate")
def activate_provider(pid: str):
    _require_ai_settings_unlocked()
    ai.set_active(pid)
    return {"ok": True}


@app.post("/api/providers/test")
def test_provider(body: ProviderIn):
    """用表单当前内容直接测连通（未保存也能测）。若没填 key 则用已存的。"""
    _require_ai_settings_unlocked()
    p = body.dict()
    if not p.get("api_key") and p.get("id"):
        stored = next((x for x in ai.load_config().get("providers", []) if x["id"] == p["id"]), None)
        if stored:
            p["api_key"] = stored.get("api_key", "")
    return ai.test_connection(p)


@app.get("/api/model-profiles")
def model_profiles():
    return {"items": ai.list_model_profiles()}


@app.get("/api/ai/calls")
def recent_ai_calls(limit: int = 50):
    return {"items": ai.list_recent_calls(limit)}


@app.get("/api/ai/status")
def ai_foundation_status():
    return ai.foundation_status()


@app.put("/api/model-profiles/{profile_key}")
def update_model_profile(profile_key: str, body: ModelProfileIn):
    _require_ai_settings_unlocked()
    try:
        provider_ids = body.provider_ids or ([body.provider_id] if body.provider_id else [])
        ai.set_model_profile_chain(profile_key, provider_ids)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))
    return {"items": ai.list_model_profiles()}


# ─── 经历 / 项目 ──────────────────────────────────────────────────────────────

@app.get("/api/experiences")
def list_experiences():
    return db.get_experiences()


@app.get("/api/experiences/{eid}")
def get_experience(eid: int):
    item = db.get_experience(eid)
    if not item:
        raise HTTPException(404, "经历不存在")
    item["projects"] = db.list_experience_projects(eid)
    return item


@app.post("/api/experiences")
def add_experience(body: ExperienceIn):
    eid = db.create_experience(body.dict())
    _commit(f"新增经历：{body.company} · {body.role}")
    return {"id": eid}


@app.get("/api/projects")
def get_projects(kind: Optional[str] = None, experience_id: Optional[int] = None):
    if kind not in (None, "work", "personal"):
        raise HTTPException(422, "kind 必须是 work 或 personal")
    return {"items": db.list_projects(kind=kind, experience_id=experience_id)}


@app.put("/api/experiences/{eid}")
def edit_experience(eid: int, body: ExperienceIn):
    previous = db.get_experience(eid)
    if not db.update_experience(eid, body.dict()):
        raise HTTPException(404, "经历不存在")
    if previous and (previous.get("company") or "").strip() != body.company.strip():
        _copy_company_logo(previous.get("company") or "", body.company)
    _commit(f"编辑经历：{body.company} · {body.role}")
    return {"item": get_experience(eid)}


@app.get("/api/experiences/{eid}/projects")
def get_experience_projects(eid: int):
    if not db.get_experience(eid):
        raise HTTPException(404, "经历不存在")
    return {"items": db.list_experience_projects(eid)}


@app.get("/api/projects/{pid}")
def get_project(pid: int):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@app.delete("/api/experiences/{eid}")
def del_experience(eid: int):
    if not db.delete_experience(eid):
        raise HTTPException(404, "经历不存在")
    _commit("删除一段经历及其项目")
    return {"ok": True}


@app.post("/api/experiences/{eid}/projects")
def add_project(eid: int, body: ProjectIn):
    pid = db.create_project(eid, body.dict())
    if not pid:
        raise HTTPException(404, "经历不存在")
    _workspace_event("project_created", "project", pid, f"新增项目：{body.name}", "project", pid)
    _commit(f"新增项目：{body.name}")
    return {"id": pid}


@app.put("/api/projects/{pid}")
def edit_project(pid: int, body: ProjectIn):
    if not db.update_project(pid, body.dict()):
        raise HTTPException(404, "项目不存在")
    _workspace_event("project_updated", "project", pid, f"用户更新项目：{body.name}", "project", pid)
    _commit(f"编辑项目文档：{body.name}")
    return {"ok": True}


@app.delete("/api/projects/{pid}")
def del_project(pid: int):
    p = db.get_project(pid) or {}
    if not p or not db.delete_project(pid):
        raise HTTPException(404, "项目不存在")
    _commit(f"删除项目：{p.get('name','')}")
    return {"ok": True}


# ─── 作品集（个人项目） ───────────────────────────────────────────────────────

@app.get("/api/portfolio")
def list_portfolio():
    return {"items": db.list_portfolio(), "build_status": db.PORTFOLIO_STATUS}


@app.post("/api/portfolio")
def add_portfolio(body: PortfolioIn):
    pid = db.create_portfolio_project(body.dict())
    _commit(f"新增作品：{body.name}")
    return {"id": pid}


@app.put("/api/portfolio/{pid}/meta")
def edit_portfolio_meta(pid: int, body: PortfolioMetaIn):
    db.update_portfolio_meta(pid, body.dict())
    p = db.get_project(pid) or {}
    _commit(f"更新作品信息：{p.get('name','')}")
    return {"ok": True}


@app.delete("/api/portfolio/{pid}")
def delete_portfolio(pid: int):
    item = db.get_project(pid)
    if not item or item.get("kind") != "personal":
        raise HTTPException(404, "作品不存在")
    if not db.delete_project(pid):
        raise HTTPException(404, "作品不存在")
    _commit(f"删除作品：{item.get('name') or pid}")
    return {"ok": True}


def _fetch_github_context(url: str):
    """抓取公开 GitHub 仓库的简介/语言/README，拼成 AI 可读的材料。"""
    import base64

    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)", url or "")
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    H = {"User-Agent": "Caddie", "Accept": "application/vnd.github+json"}
    info, langs, readme = {}, {}, ""
    try:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=H, timeout=15)
        if r.status_code == 200:
            info = r.json()
    except Exception:
        pass
    try:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/languages", headers=H, timeout=10)
        if r.status_code == 200:
            langs = r.json()
    except Exception:
        pass
    # GitHub 的 README API 会自动识别文件名和默认分支，比逐个请求 raw/HEAD 稳定且更快。
    try:
        r = requests.get(f"https://api.github.com/repos/{owner}/{repo}/readme", headers=H, timeout=15)
        if r.status_code == 200:
            payload = r.json()
            encoded = payload.get("content") or ""
            if payload.get("encoding") == "base64" and encoded:
                readme = base64.b64decode(encoded).decode("utf-8", errors="ignore")[:8000]
    except Exception:
        pass
    if not info and not readme:
        return None
    parts = [f"GitHub 仓库：{owner}/{repo}"]
    if info.get("description"):
        parts.append("仓库简介：" + info["description"])
    if info.get("homepage"):
        parts.append("项目主页：" + info["homepage"])
    if langs:
        parts.append("语言构成：" + ", ".join(langs.keys()))
    if info.get("topics"):
        parts.append("标签：" + ", ".join(info["topics"]))
    if readme:
        parts.append("README 内容：\n" + readme)
    return {"text": "\n".join(parts),
            "repo_url": info.get("html_url") or url,
            "demo_url": info.get("homepage") or ""}


PORTFOLIO_DRAFT_PROMPT = """你是作品集助手。根据用户提供的项目材料（GitHub 信息或需求/技术文档），提炼成一个【个人作品】条目。
材料可能是已完成的项目，也可能是还没动手、只有需求/设计文档的项目——都照样提炼，没做的就按规划来写，并把状态标成在做。
返回严格 JSON，不要加 markdown 代码块：
{
 "name": "作品名称（简洁）",
 "one_liner": "一句话亮点，口语化、说人话、别堆术语，突出它解决什么问题或有什么意思",
 "technologies": "用到的技术，逗号分隔，写通俗常见的名字（如 React, Python, Claude API）",
 "document": "结构化介绍，用这几个二级标题：## 这是什么\\n## 做了什么（或：计划做什么）\\n## 技术亮点\\n## 现状。通俗中文，到面试能讲出来的程度",
 "build_status": "building 在做 / live 已上线 / paused 搁置，从材料判断，拿不准就 building"
}"""


@app.post("/api/portfolio/draft")
def portfolio_draft(body: PortfolioDraftIn):
    repo_url = (body.github_url or "").strip()
    demo_url = ""
    if repo_url:
        ctx = _fetch_github_context(repo_url)
        if not ctx:
            raise HTTPException(400, "GitHub 链接解析失败，确认是 https://github.com/用户/仓库 这种格式，且仓库是公开的")
        material, repo_url, demo_url = ctx["text"], ctx["repo_url"], ctx["demo_url"]
    elif (body.doc_text or "").strip():
        material = body.doc_text.strip()[:8000]
    else:
        raise HTTPException(400, "给个 GitHub 链接，或粘贴一段项目文档")
    raw = ai.chat([{"role": "user", "content": material}], system=PORTFOLIO_DRAFT_PROMPT,
                  max_tokens=1500, profile_key="writing")
    data = ai.extract_json(raw)
    if not isinstance(data, dict):
        raise HTTPException(500, "AI 解析失败，请重试")
    data.setdefault("build_status", "building")
    data["repo_url"] = repo_url
    data["demo_url"] = data.get("demo_url") or demo_url
    return data


# ─── 面试错题集/考点库（自迭代 prompt 内容层） ────────────────────────────────

FOCUS_INGEST_PROMPT = """你在维护一个【面试错题集/考点库】。类比考试：一场面试是一张试卷，面试官的问题是题目，用户当场回答是答案，面试官追问/建议/沉默/质疑是批改，复盘要沉淀出"出题意图、失分原因、下次订正规则"。

给你两样东西：① 现有错题/考点库；② 一份新的面试原卷（逐字稿、HR反馈、用户复盘都可能混在一起）。
任务分两层：
A. 先把这场面试还原成一张"试卷"，整理面试官问题、用户回答、面试官意图、失分点和建议答法。
B. 再从这张试卷里提炼可复用的错题和考点，与现有库合并去重，输出更新后的【完整】错题/考点库。

规则：
1. dimension 写成"科目/考点"，例如"产品定义题/用户与场景"、"项目追问题/贡献边界"、"AI作品题/必要性与差异化"、"PMO题/产研闭环"。
2. directive 写成"下次答题订正规则"，例如"先说用户、场景和问题价值，再讲功能和技术"，不要写成干巴巴的标签。
3. evidence 尽量保留"题目/追问/批改原话"，方便用户回看自己为什么失分。
4. 同一考点反复出现、或会直接影响面试通过 → strength 设 'hard'（必答/铁律）；一般提醒设 'soft'。
5. 既有库里已有的别重复；若新原卷强化了它，可升级 strength 并补充更具体的 evidence。
6. 控制在 12 条以内，最重要、最容易再次出题的在前。
7. question_cards 不要编造原卷没有出现的问题；如果用户没有完整回答，answer 写"未记录"。
8. pass_prediction 必须基于证据，不要玄学打分；如果材料不足，confidence 低一点，并说明缺什么。

只返回 JSON，不要 markdown：
{"paper":{
  "title":"本场面试试卷标题",
  "role_type":"产品/AI产品/PMO/投行/资管/其他",
  "round":"一面/二面/HR面/未知",
  "summary":"这场面试主要考什么、整体表现如何",
  "question_cards":[
    {"question":"面试官原问题或规范化问题","answer":"用户当场回答摘要","intent":"面试官真实考察意图","answer_review":"这个回答好在哪里/问题在哪里","better_answer":"下次更好的回答方向","risk_level":"high/medium/low","ability":"产品理解/项目深挖/业务认知/沟通协作/AI工具/求职动机/其他"}
  ]
},
"pass_prediction":{"probability":55,"level":"uncertain","positive_signals":["..."],"risks":["..."],"missing_evidence":["..."],"confidence":0.62},
"focus_points":[
  {"dimension":"必要性/差异化","directive":"主动说明为什么要单独做这个作品、跟现成工具(ChatGPT/Notion等)的区别","strength":"hard","evidence":"随便找个大模型APP都能讨论，你优势在哪"}
]}"""


def _focus_track_id(scope: str | None):
    m = re.match(r"^track:(\d+)$", scope or "")
    return int(m.group(1)) if m else None


def _md_lines(items):
    if not isinstance(items, list):
        return "- 暂无"
    lines = [f"- {str(x).strip()}" for x in items if str(x).strip()]
    return "\n".join(lines) or "- 暂无"


def _build_interview_review_markdown(paper, prediction, focus_points, source_text):
    paper = paper or {}
    prediction = prediction or {}
    questions = paper.get("question_cards") if isinstance(paper.get("question_cards"), list) else []
    title = paper.get("title") or "面试复盘报告"
    role_type = paper.get("role_type") or "未识别"
    round_name = paper.get("round") or "未知轮次"
    prob = prediction.get("probability")
    try:
        prob_text = f"{max(0, min(100, round(float(prob))))}/100"
    except (TypeError, ValueError):
        prob_text = "未判断"
    level = prediction.get("level") or "uncertain"
    confidence = prediction.get("confidence")
    try:
        conf_text = f"{float(confidence):.2f}"
    except (TypeError, ValueError):
        conf_text = "未标注"
    out = [
        f"# {title}",
        "",
        "> 本文由 Caddie 根据原始面试材料自动生成，用于岗位知识库留痕。结构化题目已同步写入面试总库，后续可按岗位、行业、能力标签复用。",
        "",
        "## 一、整体判断",
        "",
        f"- 岗位类别：{role_type}",
        f"- 面试轮次：{round_name}",
        f"- 通过预测：{prob_text}（{level}，置信度 {conf_text}）",
        f"- 整体结论：{paper.get('summary') or '暂未生成整体结论'}",
        "",
        "## 二、正向信号",
        "",
        _md_lines(prediction.get("positive_signals")),
        "",
        "## 三、主要风险",
        "",
        _md_lines(prediction.get("risks")),
        "",
        "## 四、缺少证据",
        "",
        _md_lines(prediction.get("missing_evidence")),
        "",
        "## 五、逐题复盘",
        "",
    ]
    if questions:
        for i, q in enumerate(questions, 1):
            out.extend([
                f"### Q{i}. {q.get('question') or '未命名问题'}",
                "",
                f"- 我的回答：{q.get('answer') or '未记录'}",
                f"- 面试官意图：{q.get('intent') or '待补充'}",
                f"- 批改：{q.get('answer_review') or '待补充'}",
                f"- 下次答法：{q.get('better_answer') or '待补充'}",
                f"- 风险等级：{q.get('risk_level') or 'medium'}",
                f"- 能力标签：{q.get('ability') or '综合'}",
                "",
            ])
    else:
        out.extend(["- 未识别到清晰题目。建议补充更完整的逐字稿或 HR 反馈后重新拆解。", ""])
    out.extend([
        "## 六、订正规则",
        "",
    ])
    if focus_points:
        for i, item in enumerate(focus_points, 1):
            out.extend([
                f"{i}. {item.get('directive') or item.get('content') or '待补充'}",
                f"   - 考点：{item.get('dimension') or item.get('category') or '未分类'}",
                f"   - 强度：{item.get('strength') or 'soft'}",
                f"   - 证据：{item.get('evidence') or item.get('original_text') or '未记录'}",
            ])
    else:
        out.append("- 暂无")
    out.extend([
        "",
        "## 七、原始材料",
        "",
        (source_text or "未保存原始材料").strip()[:12000],
        "",
    ])
    return "\n".join(out)


def _interview_review_tags(paper, prediction):
    tags = []
    for value in (paper or {}).get("role_type"), (paper or {}).get("round"), (prediction or {}).get("level"):
        if value and str(value) not in tags:
            tags.append(str(value))
    for q in ((paper or {}).get("question_cards") or []):
        for value in (q.get("ability"), q.get("risk_level")):
            if value and str(value) not in tags:
                tags.append(str(value))
    return tags[:20]


@app.get("/api/focus-points")
def get_focus_points(scope: str = "portfolio"):
    return {"items": db.list_focus_points(scope)}


@app.post("/api/focus-points/ingest")
def ingest_focus_points(body: FocusIngestIn):
    scope = body.scope or "portfolio"
    existing = db.list_focus_points(scope)
    ex_text = "\n".join(
        f"- [{n.get('category') or ''}|{n.get('strength') or 'soft'}] {n.get('directive')}"
        for n in existing) or "（库为空）"
    user_msg = f"现有错题/考点库：\n{ex_text}\n\n新的面试原卷：\n{(body.transcript or '')[:16000]}"
    raw = ai.chat([{"role": "user", "content": user_msg}], system=FOCUS_INGEST_PROMPT,
                  max_tokens=3200, profile_key="fast")
    data = ai.extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("focus_points"), list):
        raise HTTPException(500, "AI 提炼失败，请重试")
    return {
        "items": data["focus_points"],
        "paper": data.get("paper") or {},
        "pass_prediction": data.get("pass_prediction") or {},
        "previous_count": len(existing),
    }


@app.post("/api/focus-points/apply")
def apply_focus_points(body: FocusApplyIn):
    scope = body.scope or "portfolio"
    paper = body.paper or {}
    prediction = body.pass_prediction or {}
    db.replace_focus_points(body.items, scope)
    track_id = _focus_track_id(scope)
    questions = paper.get("question_cards") if isinstance(paper.get("question_cards"), list) else []
    title = paper.get("title") or "面试复盘报告"
    knowledge_id = None
    if track_id:
        folders, _ = db.ensure_track_knowledge_folders(track_id)
        folder = next((item for item in folders if item.get("name") == "面试复盘"), None)
        knowledge_id = db.create_knowledge_item({
            "title": title,
            "content": _build_interview_review_markdown(paper, prediction, body.items, body.source_text or ""),
            "scope_type": "track",
            "track_id": track_id,
            "folder_id": folder.get("id") if folder else None,
            "topic": "面试复盘",
            "mastery": "learning",
            "status": "active",
            "source_type": "interview_review",
        })
    review_id = db.create_interview_review({
        "scope": scope,
        "track_id": track_id,
        "title": title,
        "role_type": paper.get("role_type"),
        "round": paper.get("round"),
        "source_text": body.source_text or "",
        "summary": paper.get("summary"),
        "prediction_json": prediction,
        "report_knowledge_id": knowledge_id,
        "tags": _interview_review_tags(paper, prediction),
    }, questions)
    _commit(f"保存面试试卷复盘：{title}")
    return {"ok": True, "count": len(body.items), "review_id": review_id,
            "knowledge_id": knowledge_id, "questions": len(questions)}


@app.get("/api/interview/reviews")
def interview_review_library(track_id: Optional[int] = None, role_type: Optional[str] = None,
                             q: Optional[str] = None, limit: int = 100):
    return {"items": db.list_interview_reviews(track_id=track_id, role_type=role_type,
                                               q=q, limit=limit)}


# ─── 秋招机会库 ───────────────────────────────────────────────────────────────

def _opportunity_payload(body: JobOpportunityIn):
    d = body.dict()
    if body.rules:
        d["rules"] = body.rules.dict()
    return d


OPPORTUNITY_PARSE_PROMPT = """你是 Caddie 的秋招情报解析器。用户会粘贴来自小红书、牛客、微信公众号、官网、表格或招聘页面的内容。
任务：把内容解析成【投递前机会库】候选项，不要把它当成已投递。

抽取规则：
1. 可以有多个岗位就输出多个 items；只有公司没岗位时 role 写"岗位待确认"。
2. batch_type 只能是 early/autumn/supplement/spring/intern/unknown。
3. status 默认 new；priority 只能 high/normal/low；fit_score 不确定填 0。
4. deadline_date 必须是 YYYY-MM-DD；只有月日时按当前年份 {current_year} 年推断；不确定填 null。
5. flow_days 是自然日全流程耗时，默认 4；如果出现测评/笔试/材料复杂可填 5-7。
6. rules 中的字段未知就写 unknown，不要猜。
7. 如果文本明确说"不影响正式批"，early_batch_impact=safe；"会影响/会卡/面评影响/只能一次"则 risky 或 blocks。
8. locks_choice 表示是否锁公司/岗位/志愿；multi_apply_allowed 表示是否允许多岗位/多志愿。
9. evidence 写最短证据句，能让用户回看来源判断可信度。

只返回 JSON，不要 markdown：
{"items":[
  {
    "company":"公司",
    "role":"岗位",
    "direction":"方向/行业/职能",
    "company_industry":"行业/求职板块",
    "company_type":"企业类型",
    "batch_type":"early",
    "status":"new",
    "priority":"normal",
    "fit_score":0,
    "source_type":"xiaohongshu/nowcoder/weixin/official/manual",
    "source_title":"来源标题",
    "source_url":"来源 URL",
    "apply_url":"投递链接或 null",
    "location":"地点或 null",
    "deadline_date":"2026-08-31 或 null",
    "deadline_type":"hard/soft/unknown",
    "flow_days":5,
    "buffer_days":1,
    "jd":"与这个岗位直接相关的原文摘要或 JD",
    "notes":"机会判断与风险备注",
    "rules":{
      "rule_status":"unverified/verified/conflicting",
      "early_batch_impact":"unknown/safe/risky/blocks",
      "locks_choice":"unknown/yes/no/partial",
      "multi_apply_allowed":"unknown/yes/no/partial",
      "cooldown_days":null,
      "rolling_review":"unknown/yes/no/partial",
      "referral_required":"unknown/yes/no/partial",
      "resume_editable":"unknown/yes/no/partial",
      "assessment_trigger":"投递后立即/初筛后/未知/null",
      "evidence":"证据句"
    }
  }
]}
""".replace("{current_year}", str(datetime.now().year))


def _fetch_opportunity_source(url: str) -> str:
    if not url:
        return ""
    def clean_html(html: str) -> str:
        text = re.sub(r"<(script|style|noscript|svg)[\s\S]*?</\1>", " ", html or "", flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        r.raise_for_status()
        text = clean_html(r.text)
        if len(text) >= 180:
            return text[:24000]
    except Exception:
        pass
    try:
        reader_url = "https://r.jina.ai/" + url
        r = requests.get(reader_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = re.sub(r"\s+", " ", r.text or "").strip()
        return text[:24000] if len(text) >= 80 else ""
    except Exception:
        return ""


def _normalize_date_text(text: str):
    if not text:
        return None
    m = re.search(r"(20\d{2})[./\-年]\s*(\d{1,2})[./\-月]\s*(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})[./\-月]\s*(\d{1,2})", text)
        if not m:
            return None
        y = datetime.now().year
        mo, d = map(int, m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except Exception:
        return None


def _pick_deadline(text: str):
    patterns = [
        r"(?:截止|ddl|DDL|网申截止|投递截止|截止时间|截止日期)[：:\s]*(.{0,24})",
        r"(\d{4}[./\-年]\s*\d{1,2}[./\-月]\s*\d{1,2})",
        r"(\d{1,2}[./\-月]\s*\d{1,2})",
    ]
    for p in patterns:
        for m in re.finditer(p, text or "", re.I):
            d = _normalize_date_text(m.group(1) if m.groups() else m.group(0))
            if d:
                return d
    return None


def _infer_batch_type(text: str):
    s = text or ""
    if "补录" in s:
        return "supplement"
    if "提前批" in s:
        return "early"
    if "春招" in s:
        return "spring"
    if "实习" in s or "暑期" in s:
        return "intern"
    if "秋招" in s or "校招" in s:
        return "autumn"
    return "unknown"


def _infer_company_type(company: str, text: str = ""):
    s = f"{company or ''} {text or ''}"
    if re.search(r"头部券商|大型券商|证券公司|券商|DCM|债券承销|定价发行|簿记建档", s, re.I):
        return "头部券商"
    if re.search(r"国企|央企|中粮|中金|中信|国投|华润|招商局|国家|中国银行|工商银行|建设银行|农业银行|交通银行|邮储|政策性", s):
        return "国央企"
    if re.search(r"银行|证券|基金|资管|信托|投行|券商|期货|财富|金融", s):
        return "金融机构"
    if re.search(r"保险|人寿|财险|养老|健康险", s):
        return "保险/资管"
    if re.search(r"腾讯|阿里|字节|美团|拼多多|京东|百度|网易|快手|小红书|B站|哔哩|携程|滴滴|互联网|电商|游戏|云", s, re.I):
        return "互联网"
    if re.search(r"华为|小米|OPPO|vivo|联想|大疆|科大讯飞|商汤|旷视|科技|AI|人工智能|机器人", s, re.I):
        return "科技/硬件"
    if re.search(r"汽车|蔚来|小鹏|理想|比亚迪|吉利|长城|上汽|广汽|车企|新能源", s):
        return "汽车/新能源"
    if re.search(r"宝洁|联合利华|欧莱雅|可口可乐|雀巢|快消|消费品|零售", s):
        return "消费/零售"
    if re.search(r"外企|global|international|consulting|咨询|麦肯锡|贝恩|BCG|德勤|普华|安永|毕马威", s, re.I):
        return "外企/咨询"
    return "待归类"


def _infer_company_industry(company: str, role: str, text: str = ""):
    s = f"{company or ''} {role or ''} {text or ''}"
    if re.search(r"DCM|债券|定价发行|簿记建档|承销|固收", s, re.I):
        return "金融相关"
    if re.search(r"产品经理|产品策划|产品运营|AI产品|用户产品|商业产品", s, re.I):
        return "产品"
    if re.search(r"数据|分析|算法|机器学习|模型|AI|人工智能|大模型|策略", s, re.I):
        return "AI/数据"
    if re.search(r"投行|行研|研究|投资|资管|固收|交易|债券|基金|证券|银行|金融|财富", s):
        return "金融相关"
    if re.search(r"运营|增长|内容|用户|社群|市场|品牌|营销", s):
        return "运营/市场"
    if re.search(r"研发|工程师|开发|后端|前端|客户端|测试|硬件|嵌入式|技术", s, re.I):
        return "技术研发"
    if re.search(r"管培|管理培训|战略|经营|商业分析|咨询", s):
        return "综合/管培"
    if re.search(r"汽车|新能源|供应链|制造|工程|生产", s):
        return "汽车/制造"
    return "待归类"


def _classify_opportunity_fields(item: dict):
    text = " ".join(str(item.get(k) or "") for k in ("company", "role", "direction", "jd", "notes", "source_title"))
    if not item.get("company_type"):
        item["company_type"] = _infer_company_type(item.get("company") or "", text)
    if not item.get("company_industry"):
        item["company_industry"] = item.get("direction") or _infer_company_industry(item.get("company") or "", item.get("role") or "", text)
    if not item.get("direction"):
        item["direction"] = item.get("company_industry") or ""
    return item


def _extract_labeled_value(text: str, labels: tuple[str, ...], max_len: int = 120):
    for label in labels:
        m = re.search(rf"{re.escape(label)}[：:\s]+([^\n\r]+)", text or "")
        if m:
            return m.group(1).strip()[:max_len]
    return ""


def _extract_apply_contact(text: str):
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    if email:
        return "mailto:" + email.group(0)
    url = next((m.group(0) for m in re.finditer(r"https?://[^\s)）\]】>\"']+", text or "") if "xiaohongshu.com" not in m.group(0)), "")
    return url or ""


def _structured_jd_from_text(text: str):
    fields = [
        ("工作内容", ("工作内容", "岗位职责", "职责")),
        ("专业要求", ("专业要求", "任职要求", "岗位要求", "要求")),
        ("时间要求", ("时间要求", "实习时间", "到岗时间")),
        ("实习地点", ("实习地点", "工作地点", "地点")),
        ("投递方式", ("投递方式", "申请方式", "简历投递")),
    ]
    parts = []
    for name, labels in fields:
        value = _extract_labeled_value(text, labels, 500)
        if value:
            parts.append(f"{name}：{value}")
    if "简历命名" in (text or ""):
        m = re.search(r"简历命名为[“\"]?([^”\"\n]+)", text)
        if m:
            parts.append(f"简历命名：{m.group(1).strip()}")
    return "\n".join(parts) or (text or "")[:3000]


def _infer_company_role(text: str, title: str = ""):
    s = "\n".join(x for x in [title, text] if x)
    company = ""
    role = _extract_labeled_value(s, ("实习岗位", "招聘岗位", "岗位名称"), 60)
    m = re.search(r"(?:实习机构|机构|公司|企业|单位)[：:\s]+([^\n，,。；;｜|]{2,40})", s)
    if m:
        company = m.group(1)
    if not company:
        m = re.search(r"([A-Za-z0-9\u4e00-\u9fa5]{2,24})(?:集团|公司|银行|证券|基金|保险|科技|互娱|音乐|云|汽车|能源|创新|中金|华泰|腾讯|阿里|字节|美团|拼多多|小米|华为|网易|京东|百度)", s)
        if m:
            company = m.group(0)
    if not company:
        first = next((x.strip() for x in s.splitlines() if x.strip()), "")
        company = re.split(r"[｜|·\-—:：]", first)[0][:24] or "公司待确认"
    if not role:
        m = re.search(r"(?:实习岗位|岗位|职位|招聘)[：:\s]+([^\n，,。；;]{2,60})", s)
        if m:
            role = m.group(1).strip()
    if not role:
        m = re.search(r"([A-Za-z0-9\u4e00-\u9fa5]{2,32}(?:工程师|产品经理|管培生|分析师|研究员|运营|实习生|开发|算法|后端|前端|投行|行研|销售|市场|财务|风控))", s)
        if m:
            role = m.group(1).strip()
    return company or "公司待确认", role or "岗位待确认"


def _infer_rules(text: str):
    s = text or ""
    rules = {
        "rule_status": "unverified",
        "early_batch_impact": "unknown",
        "locks_choice": "unknown",
        "multi_apply_allowed": "unknown",
        "cooldown_days": None,
        "rolling_review": "unknown",
        "referral_required": "unknown",
        "resume_editable": "unknown",
        "assessment_trigger": None,
        "evidence": "",
    }
    if re.search(r"不影响.{0,12}正式批|正式批.{0,12}不受影响", s):
        rules["early_batch_impact"] = "safe"
        rules["rule_status"] = "verified"
    if re.search(r"影响.{0,12}正式批|面评|投不了正式批|卡住|提前批.{0,12}正式批.{0,12}只能|只能投递?1次|只能投一次", s):
        rules["early_batch_impact"] = "risky"
    if re.search(r"只能投(?:递)?1个|只能投(?:递)?一个|每位同学只可投|最大限度|只能投一次|投递1次", s):
        rules["locks_choice"] = "yes"
        rules["multi_apply_allowed"] = "no"
    elif re.search(r"可投两个志愿|可投2个|多个志愿|多岗位", s):
        rules["multi_apply_allowed"] = "yes"
    if "内推" in s:
        rules["referral_required"] = "partial"
    if re.search(r"滚动|先到先得|招满即止", s):
        rules["rolling_review"] = "yes"
    if re.search(r"测评|笔试", s):
        rules["assessment_trigger"] = "投递后或初筛后触发，需预留时间"
    if re.search(r"免笔试", s):
        rules["assessment_trigger"] = "免笔试"
    evidence = []
    for line in re.split(r"[\n。；;]", s):
        if any(k in line for k in ("正式批", "只能", "志愿", "内推", "测评", "笔试", "截止", "滚动")):
            evidence.append(line.strip())
        if len(evidence) >= 3:
            break
    rules["evidence"] = "；".join(evidence)[:500]
    return rules


def _heuristic_parse_opportunities(material: str, source_type: str, source_title: str, source_url: str):
    text = (material or "").strip()
    if not text:
        return []
    company, role = _infer_company_role(text, source_title or "")
    batch = _infer_batch_type(text)
    deadline = _pick_deadline(text)
    apply_url = _extract_apply_contact(text)
    location = _extract_labeled_value(text, ("实习地点", "工作地点", "地点"), 80)
    flow_days = 6 if re.search(r"测评|笔试|附件|作品|材料", text) else 4
    jd_text = _structured_jd_from_text(text)
    notes = []
    for label in ("时间要求", "实习地点", "投递方式"):
        value = _extract_labeled_value(text, (label,), 300)
        if value:
            notes.append(f"{label}：{value}")
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if email:
        notes.append(f"投递邮箱：{email.group(0)}")
    resume_name = re.search(r"简历命名为[“\"]?([^”\"\n]+)", text)
    if resume_name:
        notes.append(f"简历命名：{resume_name.group(1).strip()}")
    note_text = "\n".join(notes) or "启发式解析生成，请核验公司、岗位和规则。"
    if role == "岗位待确认":
        chunks = [x.strip() for x in re.split(r"[\n；;。]", text) if x.strip()]
        roles = []
        for c in chunks[:80]:
            m = re.search(r"([A-Za-z0-9\u4e00-\u9fa5]{2,32}(?:工程师|产品经理|管培生|分析师|研究员|运营|实习生|开发|算法|后端|前端|投行|行研|销售|市场|财务|风控))", c)
            if m and m.group(1) not in roles:
                roles.append(m.group(1))
        if len(roles) > 1:
            jd_excerpt = text[:2000]
            return [{
                "company": company,
                "role": r,
                "direction": "",
                "batch_type": batch,
                "status": "new",
                "priority": "normal",
                "fit_score": 0,
                "source_type": source_type,
                "source_title": source_title,
                "source_url": source_url,
                "apply_url": apply_url,
                "location": location or None,
                "deadline_date": deadline,
                "deadline_type": "hard" if deadline else "unknown",
                "flow_days": flow_days,
                "buffer_days": 1,
                "jd": jd_excerpt,
                "notes": note_text,
                "rules": _infer_rules(text),
            } for r in roles[:8]]
    return [{
        "company": company,
        "role": role,
        "direction": "",
        "batch_type": batch,
        "status": "new",
        "priority": "normal",
        "fit_score": 0,
        "source_type": source_type,
        "source_title": source_title,
        "source_url": source_url,
        "apply_url": apply_url,
        "location": location or None,
        "deadline_date": deadline,
        "deadline_type": "hard" if deadline else "unknown",
        "flow_days": flow_days,
        "buffer_days": 1,
        "jd": jd_text,
        "notes": note_text,
        "rules": _infer_rules(text),
    }]


def _clean_parsed_opportunity(item: dict, defaults: dict):
    item = _classify_opportunity_fields(dict(item or {}))
    company = (item.get("company") or "").strip() or "公司待确认"
    role = (item.get("role") or "").strip() or "岗位待确认"
    batch = item.get("batch_type") if item.get("batch_type") in db.OPPORTUNITY_BATCH_LABEL else "unknown"
    status = item.get("status") if item.get("status") in db.OPPORTUNITY_STATUS_LABEL else "new"
    priority = item.get("priority") if item.get("priority") in {"high", "normal", "low"} else "normal"
    rules = item.get("rules") if isinstance(item.get("rules"), dict) else {}
    clean = {
        "company": company[:80],
        "role": role[:120],
        "direction": item.get("direction") or "",
        "company_industry": item.get("company_industry") or "",
        "company_type": item.get("company_type") or "",
        "batch_type": batch,
        "status": status,
        "priority": priority,
        "fit_score": max(0, min(100, int(item.get("fit_score") or 0))),
        "source_type": item.get("source_type") or defaults.get("source_type") or "manual",
        "source_title": item.get("source_title") or defaults.get("source_title") or "",
        "source_url": item.get("source_url") or defaults.get("source_url") or "",
        "apply_url": item.get("apply_url") or "",
        "location": item.get("location") or "",
        "deadline_date": _normalize_date_text(item.get("deadline_date") or "") or None,
        "deadline_type": item.get("deadline_type") or "unknown",
        "flow_days": max(0, min(30, int(item.get("flow_days") or 4))),
        "buffer_days": max(0, min(14, int(item.get("buffer_days") or 1))),
        "jd": item.get("jd") or "",
        "notes": item.get("notes") or "",
        "rules": {
            "rule_status": rules.get("rule_status") or "unverified",
            "early_batch_impact": rules.get("early_batch_impact") or "unknown",
            "locks_choice": rules.get("locks_choice") or "unknown",
            "multi_apply_allowed": rules.get("multi_apply_allowed") or "unknown",
            "cooldown_days": rules.get("cooldown_days"),
            "rolling_review": rules.get("rolling_review") or "unknown",
            "referral_required": rules.get("referral_required") or "unknown",
            "resume_editable": rules.get("resume_editable") or "unknown",
            "assessment_trigger": rules.get("assessment_trigger"),
            "evidence": rules.get("evidence") or item.get("evidence") or "",
        },
    }
    return clean


QIUZHAO_FEED_REPO_URL = "https://github.com/xixicc186/xixicc2027"
QIUZHAO_FEED_JOBS_URL = "https://raw.githubusercontent.com/xixicc186/xixicc2027/main/jobs.json"
QIUZHAO_FEED_SKILL_URL = "https://github.com/xixicc186/xixicc2027/tree/main/skill/qiuzhao-feed"
JOB_RADAR_REPO_URL = "https://github.com/Jasmine-Liu-min/job-radar"
JOB_RADAR_JOBS_URL = "https://raw.githubusercontent.com/Jasmine-Liu-min/job-radar/main/data/jobs.json"
JOB_RADAR_HEALTH_URL = "https://raw.githubusercontent.com/Jasmine-Liu-min/job-radar/main/data/health_report.json"
JOB_RADAR_PAGE_URL = "https://github.com/Jasmine-Liu-min/job-radar/blob/main/data/jobs.html"
_JOB_RADAR_CACHE = {"ts": None, "jobs": None, "health": None}


def _qiuzhao_feed_batch_type(value: str):
    s = str(value or "")
    if "提前" in s:
        return "early"
    if "补" in s:
        return "supplement"
    if "春" in s:
        return "spring"
    if "实习" in s or "暑期" in s:
        return "intern"
    if "正式" in s or "秋招" in s or "校招" in s:
        return "autumn"
    return "unknown"


def _qiuzhao_feed_company_type(company: str, industry: str):
    s = f"{company or ''} {industry or ''}"
    if "央国企" in s or "军工" in s or "事业单位" in s:
        return "国央企"
    if "银行" in s or "金融" in s:
        return "金融机构"
    if "互联网" in s or "游戏" in s:
        return "互联网"
    if "半导体" in s or "硬件" in s or "科技" in s:
        return "科技/硬件"
    if "车企" in s or "汽车" in s or "新能源" in s:
        return "汽车/新能源"
    if "快消" in s or "零售" in s:
        return "消费/零售"
    if "外企" in s:
        return "外企/咨询"
    return _infer_company_type(company, s)


def _qiuzhao_feed_role(job: dict):
    program = str(job.get("program") or "").strip()
    positions = [str(x).strip() for x in (job.get("positions") or []) if str(x).strip()]
    if program and positions:
        detail = "、".join(positions[:3])
        suffix = "等" if len(positions) > 3 else ""
        return f"{program} · {detail}{suffix}"[:120]
    if program:
        return program[:120]
    if positions:
        detail = "、".join(positions[:4])
        suffix = "等" if len(positions) > 4 else ""
        return f"{detail}{suffix}"[:120]
    return "岗位待确认"


def _qiuzhao_feed_apply_url(job: dict):
    for key in ("apply_url", "official_wechat"):
        value = str(job.get(key) or "").strip()
        if value.startswith(("http://", "https://", "mailto:")):
            return value
    return ""


def _qiuzhao_feed_to_opportunity(job: dict):
    company = str(job.get("company") or "").strip() or "公司待确认"
    industry = str(job.get("industry") or "").strip() or "待归类"
    role = _qiuzhao_feed_role(job)
    positions = [str(x).strip() for x in (job.get("positions") or []) if str(x).strip()]
    location = "、".join(str(x).strip() for x in (job.get("locations") or []) if str(x).strip())
    deadline = _normalize_date_text(str(job.get("deadline") or "")) or None
    confirmed_by = int(job.get("confirmed_by") or 0) if str(job.get("confirmed_by") or "").isdigit() else 0
    evidence_parts = [
        f"数据集：xixicc2027 / qiuzhao-feed",
        f"行业：{industry}",
        f"批次：{job.get('batch') or '待确认'}",
        f"届别：{job.get('cohort') or '不限'}",
    ]
    if confirmed_by:
        evidence_parts.append(f"多渠道确认：{confirmed_by}")
    jd_parts = [
        f"项目：{job.get('program') or '-'}",
        f"岗位方向：{'、'.join(positions) if positions else '-'}",
        f"地点：{location or '-'}",
        f"截止：{deadline or job.get('deadline') or '待确认'}",
        f"投递入口：{job.get('apply_url') or job.get('official_wechat') or '搜索官方公众号'}",
    ]
    return _clean_parsed_opportunity({
        "company": company,
        "role": role,
        "direction": "、".join(positions[:4]) or industry,
        "company_industry": industry,
        "company_type": _qiuzhao_feed_company_type(company, industry),
        "batch_type": _qiuzhao_feed_batch_type(job.get("batch")),
        "status": "new",
        "priority": "high" if deadline and deadline <= (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d") else "normal",
        "fit_score": 0,
        "source_type": "skill",
        "source_title": "qiuzhao-feed · xixicc2027 每日秋招数据",
        "source_url": QIUZHAO_FEED_REPO_URL,
        "apply_url": _qiuzhao_feed_apply_url(job),
        "location": location,
        "deadline_date": deadline,
        "deadline_type": "hard" if deadline else "unknown",
        "flow_days": 4 if deadline else 3,
        "buffer_days": 1,
        "jd": "\n".join(jd_parts),
        "notes": "来自公开开源秋招数据集，投递前仍需打开官方入口核验岗位、批次和截止时间。",
        "rules": {
            "rule_status": "unverified",
            "early_batch_impact": "unknown",
            "locks_choice": "unknown",
            "multi_apply_allowed": "unknown",
            "rolling_review": "unknown",
            "evidence": "；".join(evidence_parts),
        },
    }, {"source_type": "skill", "source_title": "qiuzhao-feed", "source_url": QIUZHAO_FEED_REPO_URL})


def _load_qiuzhao_feed_jobs():
    try:
        r = requests.get(QIUZHAO_FEED_JOBS_URL, timeout=25, headers={"User-Agent": "Caddie/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"读取 xixicc2027 数据失败：{str(e)[:160]}")
    if not isinstance(data, list):
        raise HTTPException(502, "xixicc2027 jobs.json 格式异常")
    return [x for x in data if isinstance(x, dict)]


def _job_radar_company_type(job: dict):
    org_type = str(job.get("org_type") or "").strip().lower()
    industry = str(job.get("industry") or "")
    company = str(job.get("company_name") or "")
    mapping = {
        "internet": "互联网",
        "state_owned": "国央企",
        "state": "国央企",
        "finance": "金融机构",
        "bank": "金融机构",
        "consulting": "外企/咨询",
        "auto": "汽车/新能源",
        "hardware": "科技/硬件",
    }
    if org_type in mapping:
        return mapping[org_type]
    return _infer_company_type(company, f"{industry} {org_type}")


def _job_radar_batch_type(job: dict):
    text = " ".join([
        str(job.get("title") or ""),
        str(job.get("job_type") or ""),
        str(job.get("employment_type") or ""),
        " ".join(str(x) for x in (job.get("tags") or [])),
        str(job.get("jd_text") or "")[:500],
    ])
    if re.search(r"提前批|早鸟|预招", text):
        return "early"
    if re.search(r"补录|补招", text):
        return "supplement"
    if re.search(r"春招", text):
        return "spring"
    if re.search(r"实习|intern|暑期", text, re.I):
        return "intern"
    if re.search(r"秋招|校招|校园招聘|应届|2027届|2026届|管培", text):
        return "autumn"
    return "unknown"


def _job_radar_is_social_only(job: dict):
    text = " ".join([
        str(job.get("title") or ""),
        str(job.get("official_url") or ""),
        " ".join(str(x) for x in (job.get("tags") or [])),
    ])
    if "仅社招" in text:
        return True
    if re.search(r"/experienced/|social-recruitment|社招|社会招聘", text, re.I):
        return True
    if re.search(r"高级|资深|专家|负责人|经理-\w|leader|principal|senior", text, re.I) and not re.search(r"校招|校园|实习|应届|管培", text):
        return True
    return False


def _job_radar_keep(job: dict, scope: str):
    if job.get("gone") is True:
        return False
    if scope == "all":
        return True
    batch = _job_radar_batch_type(job)
    if scope == "intern":
        return batch == "intern"
    if scope == "campus":
        if _job_radar_is_social_only(job):
            return False
        text = " ".join([
            str(job.get("title") or ""),
            str(job.get("job_type") or ""),
            str(job.get("employment_type") or ""),
            " ".join(str(x) for x in (job.get("tags") or [])),
            str(job.get("jd_text") or "")[:800],
        ])
        if batch in {"early", "autumn", "intern", "supplement"}:
            return True
        return bool(re.search(r"2027届|2026届|校招|校园招聘|应届|管培|实习|提前批|秋招", text))
    return not _job_radar_is_social_only(job)


def _job_radar_to_opportunity(job: dict):
    company = str(job.get("company_name") or "").strip() or "公司待确认"
    role = str(job.get("title") or "").strip() or "岗位待确认"
    tags = [str(x) for x in (job.get("tags") or []) if str(x).strip()]
    industry = str(job.get("industry") or "").strip() or _infer_company_industry(company, role, " ".join(tags))
    deadline = _normalize_date_text(str(job.get("deadline") or "")) or None
    official_url = str(job.get("official_url") or "").strip()
    backup_url = str(job.get("backup_url") or "").strip()
    match_score = int(job.get("match_score") or 0) if str(job.get("match_score") or "").lstrip("-").isdigit() else 0
    source_confidence = int(job.get("source_confidence") or 0) if str(job.get("source_confidence") or "").isdigit() else 0
    fit_score = max(0, min(100, round(match_score / 3))) if match_score else 0
    jd = str(job.get("jd_text") or "").strip()
    risk_flags = [str(x) for x in (job.get("risk_flags") or []) if str(x).strip()]
    evidence_parts = [
        "数据源：Jasmine-Liu-min/job-radar",
        f"source_id：{job.get('source_id') or '-'}",
        f"信源置信度：{source_confidence}",
        f"匹配分：{match_score}",
        f"首次发现：{job.get('first_seen') or '-'}",
        f"最近发现：{job.get('last_seen') or '-'}",
    ]
    if tags:
        evidence_parts.append("标签：" + "、".join(tags[:12]))
    if risk_flags:
        evidence_parts.append("风险标记：" + "、".join(risk_flags))
    jd_parts = [
        f"岗位：{role}",
        f"地点：{job.get('location') or '待确认'}",
        f"行业：{industry}",
        f"发布时间：{job.get('publish_time') or '待确认'}",
        f"截止：{deadline or job.get('deadline') or '待确认'}",
    ]
    if jd:
        jd_parts.append(jd[:3000])
    return _clean_parsed_opportunity({
        "company": company,
        "role": role,
        "direction": "、".join([x for x in tags if not x.startswith("行业:")][:5]) or industry,
        "company_industry": industry,
        "company_type": _job_radar_company_type(job),
        "batch_type": _job_radar_batch_type(job),
        "status": "new",
        "priority": "high" if (deadline and deadline <= (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")) or fit_score >= 80 else "normal",
        "fit_score": fit_score,
        "source_type": "skill",
        "source_title": "Job Radar · 27届招聘信息台",
        "source_url": JOB_RADAR_REPO_URL,
        "apply_url": official_url or backup_url,
        "location": str(job.get("location") or ""),
        "deadline_date": deadline,
        "deadline_type": "hard" if deadline else "unknown",
        "flow_days": 5 if re.search(r"笔试|测评|算法|数据|AI|产品", role + " " + jd[:500], re.I) else 4,
        "buffer_days": 1,
        "jd": "\n".join(jd_parts),
        "notes": "来自 Job Radar 自动同步数据；已按 Caddie 过滤明显社招/下线项，投递前仍需打开官方链接核验。",
        "rules": {
            "rule_status": "unverified",
            "early_batch_impact": "unknown",
            "locks_choice": "unknown",
            "multi_apply_allowed": "unknown",
            "rolling_review": "unknown",
            "evidence": "；".join(evidence_parts),
        },
    }, {"source_type": "skill", "source_title": "Job Radar", "source_url": JOB_RADAR_REPO_URL})


def _load_job_radar_jobs():
    now_ts = datetime.now().timestamp()
    if _JOB_RADAR_CACHE.get("jobs") is not None and _JOB_RADAR_CACHE.get("ts") and now_ts - _JOB_RADAR_CACHE["ts"] < 300:
        return _JOB_RADAR_CACHE["jobs"]
    try:
        r = requests.get(JOB_RADAR_JOBS_URL, timeout=90, headers={"User-Agent": "Caddie/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"读取 Job Radar 数据失败：{str(e)[:160]}")
    if not isinstance(data, list):
        raise HTTPException(502, "Job Radar jobs.json 格式异常")
    jobs = [x for x in data if isinstance(x, dict)]
    _JOB_RADAR_CACHE["jobs"] = jobs
    _JOB_RADAR_CACHE["ts"] = now_ts
    return jobs


def _load_job_radar_health():
    now_ts = datetime.now().timestamp()
    if _JOB_RADAR_CACHE.get("health") is not None and _JOB_RADAR_CACHE.get("ts") and now_ts - _JOB_RADAR_CACHE["ts"] < 300:
        return _JOB_RADAR_CACHE["health"]
    try:
        r = requests.get(JOB_RADAR_HEALTH_URL, timeout=20, headers={"User-Agent": "Caddie/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}
    health = data if isinstance(data, dict) else {}
    _JOB_RADAR_CACHE["health"] = health
    return health


def _opportunity_parse_material(body: OpportunityParseIn):
    content = (body.content or "").strip()
    fetched = ""
    if body.source_url and len(content) < 80:
        fetched = _fetch_opportunity_source(body.source_url.strip())
    material = "\n\n".join(x for x in [
        f"来源类型：{body.source_type}",
        f"来源标题：{body.source_title or ''}",
        f"来源链接：{body.source_url or ''}",
        content,
        fetched,
    ] if x).strip()
    return material


OPPORTUNITY_SPREADSHEET_ALIASES = {
    "company": ["公司", "企业", "公司名称", "单位", "雇主", "company"],
    "role": ["岗位", "岗位名称", "职位", "职位名称", "招聘岗位", "role", "position", "job"],
    "company_industry": ["行业", "求职板块", "板块", "方向", "领域", "赛道", "industry", "sector"],
    "company_type": ["企业类型", "公司类型", "企业性质", "性质", "公司性质", "type"],
    "batch_type": ["批次", "招聘批次", "校招批次", "秋招批次", "batch"],
    "status": ["机会状态", "状态", "投递状态", "投递进度", "progress", "status"],
    "priority": ["优先级", "推荐", "推荐程度", "priority"],
    "deadline_date": ["截止", "截止日期", "网申截止", "投递截止", "deadline", "ddl"],
    "apply_url": ["投递链接", "网申链接", "申请链接", "链接", "url", "apply url"],
    "source_url": ["来源链接", "原文链接", "来源", "source"],
    "location": ["地点", "城市", "工作地点", "location"],
    "jd": ["jd", "岗位描述", "职责", "要求", "岗位职责", "任职要求"],
    "notes": ["备注", "说明", "补充", "规则", "note", "notes"],
}


def _norm_header(text: str):
    return re.sub(r"[\s_：:()（）【】\[\]<>《》]+", "", str(text or "").strip().lower())


def _field_from_header(header: str):
    h = _norm_header(header)
    if not h:
        return None
    for field, aliases in OPPORTUNITY_SPREADSHEET_ALIASES.items():
        if any(_norm_header(alias) == h for alias in aliases):
            return field
    for field, aliases in OPPORTUNITY_SPREADSHEET_ALIASES.items():
        for alias in aliases:
            a = _norm_header(alias)
            if len(a) >= 3 and a not in {"company", "status", "source"} and a in h:
                return field
    return None


def _decode_text_table(data: bytes):
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def _read_csv_rows(data: bytes, filename: str):
    text = _decode_text_table(data)
    sample = text[:4096]
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except Exception:
        pass
    return [[(c or "").strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _xlsx_col_index(ref: str):
    letters = re.sub(r"[^A-Z]", "", (ref or "").upper())
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return max(0, n - 1)


def _read_xlsx_rows(data: bytes):
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", ns):
                shared.append("".join(t.text or "" for t in si.findall(".//a:t", ns)).strip())
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in z.namelist():
            sheet_name = next((n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), "")
        if not sheet_name:
            return []
        root = ET.fromstring(z.read(sheet_name))
        out = []
        for row in root.findall(".//a:sheetData/a:row", ns):
            vals = []
            for c in row.findall("a:c", ns):
                idx = _xlsx_col_index(c.attrib.get("r", ""))
                while len(vals) < idx:
                    vals.append("")
                t = c.attrib.get("t")
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.findall(".//a:t", ns))
                else:
                    v = c.find("a:v", ns)
                    raw = v.text if v is not None else ""
                    val = shared[int(raw)] if t == "s" and str(raw).isdigit() and int(raw) < len(shared) else raw
                vals.append(str(val or "").strip())
            if any(vals):
                out.append(vals)
        return out


def _table_rows_to_opportunities(rows: list[list[str]], filename: str):
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        return []
    header_i, mapping = 0, {}
    for i, row in enumerate(rows[:8]):
        m = {idx: _field_from_header(cell) for idx, cell in enumerate(row)}
        m = {idx: f for idx, f in m.items() if f}
        if len(m) >= 2 and ("company" in m.values() or "role" in m.values()):
            header_i, mapping = i, m
            break
    if not mapping:
        mapping = {0: "company", 1: "role", 2: "company_industry", 3: "company_type", 4: "deadline_date", 5: "apply_url", 6: "notes"}
    items = []
    for row in rows[header_i + 1:]:
        d = {}
        for idx, field in mapping.items():
            if idx < len(row) and str(row[idx]).strip():
                d[field] = str(row[idx]).strip()
        if not (d.get("company") or d.get("role")):
            continue
        blob = " ".join(str(x or "") for x in row)
        d["company"] = d.get("company") or _infer_company_role(blob, filename)[0]
        d["role"] = d.get("role") or _infer_company_role(blob, filename)[1]
        d["batch_type"] = _normalize_batch_value(d.get("batch_type") or blob)
        d["status"] = _normalize_opportunity_status(d.get("status") or "")
        d["priority"] = _normalize_priority_value(d.get("priority") or blob)
        d["deadline_date"] = _normalize_date_text(d.get("deadline_date") or "") or _pick_deadline(blob)
        d["deadline_type"] = "hard" if d.get("deadline_date") else "unknown"
        d["source_type"] = "excel" if filename.lower().endswith((".xlsx", ".xls")) else "csv"
        d["source_title"] = filename
        d["flow_days"] = 6 if re.search(r"测评|笔试|材料|作品", blob) else 4
        d["buffer_days"] = 1
        d["rules"] = _infer_rules(blob)
        d["jd"] = d.get("jd") or blob[:1800]
        d["notes"] = d.get("notes") or "来自表格导入，请核验截止日期、投递链接和规则。"
        items.append(_classify_opportunity_fields(d))
    return items[:500]


def _normalize_batch_value(value: str):
    text = str(value or "")
    inferred = _infer_batch_type(text)
    return inferred if inferred != "unknown" else "autumn"


def _normalize_opportunity_status(value: str):
    s = str(value or "")
    if re.search(r"放弃|不投|忽略", s):
        return "dismissed"
    if re.search(r"已转|工作台", s):
        return "converted"
    if re.search(r"排期|准备|已投|投递", s):
        return "planned"
    if re.search(r"可投|推荐|short", s, re.I):
        return "shortlisted"
    if re.search(r"观察|待定|关注", s):
        return "watching"
    return "new"


def _normalize_priority_value(value: str):
    s = str(value or "")
    if re.search(r"高|推荐|优先|紧急|必投", s):
        return "high"
    if re.search(r"低|备选|暂缓", s):
        return "low"
    return "normal"


def _parse_opportunity_spreadsheet(data: bytes, filename: str):
    lower = (filename or "").lower()
    if lower.endswith(".xlsx"):
        rows = _read_xlsx_rows(data)
    elif lower.endswith((".csv", ".tsv", ".txt")):
        rows = _read_csv_rows(data, lower)
    else:
        raise HTTPException(400, "目前支持 .xlsx / .csv / .tsv")
    return _table_rows_to_opportunities(rows, filename or "导入表格")


def _opencli_executable():
    candidates = [
        Path.home() / ".npm-global/bin/opencli",
        Path.home() / ".agent-reach-venv/bin/opencli",
        Path("/opt/homebrew/bin/opencli"),
        Path("/usr/local/bin/opencli"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return str(p)
    return "opencli"


def _run_agent_reach_opencli(args: list[str], timeout: int = 90) -> str:
    exe = _opencli_executable()
    env = dict(os.environ)
    extra = [
        str(Path.home() / ".npm-global/bin"),
        str(Path.home() / ".agent-reach-venv/bin"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
    ]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    try:
        p = subprocess.run(
            [exe, *[str(x) for x in args]],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        raise HTTPException(502, "没有找到 OpenCLI。请确认 agent-reach/OpenCLI 已安装，或先走“从内容解析/导入Excel”。")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "平台搜索超时，请换更具体的关键词或稍后重试")
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "OpenCLI 调用失败").strip()
        raise HTTPException(502, msg[:500])
    return p.stdout or ""


def _strip_yaml_scalar(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_opencli_yaml_list(raw: str) -> list[dict]:
    lines = (raw or "").splitlines()
    items = []
    cur = None
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip() or line.lstrip().startswith("Update available:") or line.lstrip().startswith("Run:"):
            i += 1
            continue
        is_new = line.startswith("- ")
        m = re.match(r"\s*([^:]+):\s*(.*)$", line[2:] if is_new else line)
        if is_new:
            if cur:
                items.append(cur)
            cur = {}
        if cur is not None and m:
            key, value = m.group(1).strip(), m.group(2).strip()
            if value in {">-", ">", "|-", "|"}:
                parts = []
                i += 1
                while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                    if lines[i].strip():
                        parts.append(lines[i].strip())
                    i += 1
                cur[key] = " ".join(parts).strip()
                continue
            cur[key] = _strip_yaml_scalar(value)
        i += 1
    if cur:
        items.append(cur)
    return items


def _normalize_discovery_item(x: dict, source_type: str) -> dict:
    return {
        "source_type": source_type,
        "rank": int(x.get("rank") or 0) if str(x.get("rank") or "").isdigit() else x.get("rank"),
        "title": x.get("title") or x.get("name") or "未命名结果",
        "url": x.get("url") or "",
        "author": x.get("author") or "",
        "author_url": x.get("author_url") or "",
        "likes": x.get("likes") or "",
        "published_at": x.get("published_at") or "",
    }


@app.get("/api/opportunities/discover/search")
def search_opportunity_sources(source_type: str = "xiaohongshu", q: str = "", limit: int = 12):
    _require_feature_available("autumn_opportunity_library")
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "请输入搜索关键词")
    limit = max(1, min(20, int(limit or 12)))
    if source_type != "xiaohongshu":
        raise HTTPException(400, "当前已接入小红书搜索；牛客/公众号/官网请先用 URL 或正文解析")
    raw = _run_agent_reach_opencli(["xiaohongshu", "search", q, "-f", "yaml"])
    items = [_normalize_discovery_item(x, source_type) for x in _parse_opencli_yaml_list(raw)]
    items = [x for x in items if x.get("url")][:limit]
    return {"source_type": source_type, "query": q, "items": items, "raw_count": len(items)}


@app.post("/api/opportunities/discover/read")
def read_opportunity_source(body: OpportunitySourceReadIn):
    _require_feature_available("autumn_opportunity_library")
    source_type = body.source_type or "xiaohongshu"
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "缺少来源链接")
    if source_type == "xiaohongshu":
        raw = _run_agent_reach_opencli(["xiaohongshu", "note", url, "-f", "yaml"])
        fields = _parse_opencli_yaml_list(raw)
        data = {x.get("field"): x.get("value") for x in fields if x.get("field")}
        title = data.get("title") or body.title or "小红书笔记"
        content = data.get("content") or ""
        if not content:
            raise HTTPException(502, "已读取笔记，但没有拿到正文；可以打开链接后复制正文解析")
        return {"source_type": source_type, "source_title": title, "source_url": url,
                "content": content, "meta": data}
    if source_type in {"official", "weixin", "nowcoder", "manual", "other"}:
        content = _fetch_opportunity_source(url)
        if not content:
            raise HTTPException(502, "页面读取失败，请复制正文解析")
        fallback_title = {"official": "官网招聘页", "weixin": "公众号文章", "nowcoder": "牛客帖子"}.get(source_type, "网页内容")
        return {"source_type": source_type, "source_title": body.title or fallback_title,
                "source_url": url, "content": content, "meta": {}}
    raise HTTPException(400, "当前支持小红书笔记、牛客/公众号/官网 URL 读取；其他平台请复制正文解析")


@app.post("/api/opportunities/parse")
def parse_opportunities(body: OpportunityParseIn):
    _require_feature_available("autumn_opportunity_library")
    material = _opportunity_parse_material(body)
    if not material:
        raise HTTPException(400, "请粘贴笔记/帖子/公众号/官网内容，或提供可公开访问的 URL")
    defaults = {
        "source_type": body.source_type or "manual",
        "source_title": body.source_title or "",
        "source_url": body.source_url or "",
    }
    warnings = []
    mode = "ai"
    items = []
    try:
        raw = ai.chat([{"role": "user", "content": material[:24000]}],
                      system=OPPORTUNITY_PARSE_PROMPT, max_tokens=3500,
                      profile_key="fast")
        data = ai.extract_json(raw)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("AI 未返回 items")
        items = data.get("items") or []
    except Exception as e:
        mode = "heuristic"
        warnings.append(f"AI 解析不可用，已用规则兜底：{str(e)[:120]}")
        items = _heuristic_parse_opportunities(material, defaults["source_type"], defaults["source_title"], defaults["source_url"])
    cleaned = [_clean_parsed_opportunity(x, defaults) for x in items if isinstance(x, dict)]
    dedup = []
    seen = set()
    for x in cleaned:
        key = (x["company"].lower(), x["role"].lower(), x.get("deadline_date") or "")
        if key in seen:
            continue
        seen.add(key)
        dedup.append(x)
    if not dedup:
        dedup = [_clean_parsed_opportunity(x, defaults) for x in _heuristic_parse_opportunities(material, defaults["source_type"], defaults["source_title"], defaults["source_url"])]
        mode = "heuristic"
        warnings.append("未识别到稳定候选，已生成一条待确认机会")
    return {"items": dedup[:20], "mode": mode, "warnings": warnings, "chars": len(material)}


@app.post("/api/opportunities/import-file")
async def parse_opportunity_file(file: UploadFile = File(...)):
    _require_feature_available("autumn_opportunity_library")
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    items = _parse_opportunity_spreadsheet(data, file.filename or "导入表格")
    defaults = {
        "source_type": "excel" if (file.filename or "").lower().endswith(".xlsx") else "csv",
        "source_title": file.filename or "导入表格",
        "source_url": "",
    }
    cleaned = [_clean_parsed_opportunity(x, defaults) for x in items if isinstance(x, dict)]
    return {"items": cleaned, "filename": file.filename, "row_count": len(items)}


@app.get("/api/opportunities/feeds/xixicc2027/preview")
def preview_qiuzhao_feed(industry: Optional[str] = None, keyword: Optional[str] = None,
                         days: Optional[int] = None, limit: int = 120):
    _require_feature_available("autumn_opportunity_library")
    jobs = _load_qiuzhao_feed_jobs()
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = None
    if days is not None:
        days = max(0, min(180, int(days)))
        cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    industry = (industry or "").strip()
    keyword = (keyword or "").strip().lower()

    def keep(job: dict):
        if industry and industry not in str(job.get("industry") or ""):
            return False
        text = json.dumps(job, ensure_ascii=False).lower()
        if keyword and keyword not in text:
            return False
        if cutoff:
            d = _normalize_date_text(str(job.get("deadline") or ""))
            if not d or d < today or d > cutoff:
                return False
        return True

    matched_jobs = [x for x in jobs if keep(x)]

    def feed_sort_key(job: dict):
        d = _normalize_date_text(str(job.get("deadline") or ""))
        if d and d >= today:
            deadline_group = 0
            deadline_value = d
        elif not d:
            deadline_group = 1
            deadline_value = "9999-12-31"
        else:
            deadline_group = 2
            deadline_value = d
        batch_rank = {"early": 0, "autumn": 1, "intern": 2, "supplement": 3, "spring": 4, "unknown": 5}
        confirmed = int(job.get("confirmed_by") or 0) if str(job.get("confirmed_by") or "").isdigit() else 0
        return (deadline_group, deadline_value, batch_rank.get(_qiuzhao_feed_batch_type(job.get("batch")), 5), -confirmed)

    matched_jobs.sort(key=feed_sort_key)
    limit = max(1, min(500, int(limit or 120)))
    items = [_qiuzhao_feed_to_opportunity(x) for x in matched_jobs[:limit]]
    industries = sorted({str(x.get("industry") or "").strip() for x in jobs if str(x.get("industry") or "").strip()})
    latest_seen = max([str(x.get("last_seen") or "") for x in jobs] or [""])
    return {
        "items": items,
        "mode": "qiuzhao-feed",
        "chars": len(matched_jobs),
        "warnings": ["来自 xixicc186/xixicc2027 开源数据集；投递前请打开官方入口二次核验。"],
        "stats": {
            "total": len(jobs),
            "matched": len(matched_jobs),
            "returned": len(items),
            "latest_seen": latest_seen,
            "repo_url": QIUZHAO_FEED_REPO_URL,
            "skill_url": QIUZHAO_FEED_SKILL_URL,
            "jobs_url": QIUZHAO_FEED_JOBS_URL,
            "industries": industries,
        },
    }


@app.get("/api/opportunities/feeds/job-radar/preview")
def preview_job_radar_feed(industry: Optional[str] = None, keyword: Optional[str] = None,
                           days: Optional[int] = None, limit: int = 120, scope: str = "campus"):
    _require_feature_available("autumn_opportunity_library")
    jobs = _load_job_radar_jobs()
    health = _load_job_radar_health()
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = None
    if days is not None:
        days = max(0, min(180, int(days)))
        cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    industry = (industry or "").strip()
    keyword = (keyword or "").strip().lower()
    scope = scope if scope in {"campus", "intern", "all", "non_social"} else "campus"

    def keep(job: dict):
        if not _job_radar_keep(job, scope):
            return False
        if industry and industry not in str(job.get("industry") or "") and industry not in " ".join(str(x) for x in (job.get("tags") or [])):
            return False
        text = json.dumps(job, ensure_ascii=False).lower()
        if keyword and keyword not in text:
            return False
        if cutoff:
            d = _normalize_date_text(str(job.get("deadline") or ""))
            if not d or d < today or d > cutoff:
                return False
        return True

    matched_jobs = [x for x in jobs if keep(x)]

    def sort_key(job: dict):
        d = _normalize_date_text(str(job.get("deadline") or ""))
        score = int(job.get("match_score") or 0) if str(job.get("match_score") or "").lstrip("-").isdigit() else 0
        confidence = int(job.get("source_confidence") or 0) if str(job.get("source_confidence") or "").isdigit() else 0
        if d and d >= today:
            deadline_group = 0
            deadline_value = d
        elif not d:
            deadline_group = 1
            deadline_value = "9999-12-31"
        else:
            deadline_group = 2
            deadline_value = d
        seen = str(job.get("last_seen") or "")
        batch_rank = {"early": 0, "autumn": 1, "intern": 2, "supplement": 3, "spring": 4, "unknown": 5}
        if deadline_group == 1:
            return (deadline_group, batch_rank.get(_job_radar_batch_type(job), 5), -score, -confidence, seen)
        return (deadline_group, deadline_value, batch_rank.get(_job_radar_batch_type(job), 5), -score, -confidence, seen)

    matched_jobs.sort(key=sort_key)
    limit = max(1, min(500, int(limit or 120)))
    items = [_job_radar_to_opportunity(x) for x in matched_jobs[:limit]]
    industries = sorted({str(x.get("industry") or "").strip() for x in jobs if str(x.get("industry") or "").strip()})
    latest_seen = max([str(x.get("last_seen") or "") for x in jobs] or [""])
    alerts = health.get("alerts") if isinstance(health.get("alerts"), list) else []
    warnings = ["来自 Jasmine-Liu-min/job-radar；Caddie 默认过滤明显社招/下线岗位，投递前请打开官方入口二次核验。"]
    if alerts:
        warnings.append(f"Job Radar 当前有 {len(alerts)} 个信源健康提醒，部分官网抓取可能失效或下降。")
    return {
        "items": items,
        "mode": "job-radar",
        "chars": len(matched_jobs),
        "warnings": warnings,
        "stats": {
            "total": len(jobs),
            "matched": len(matched_jobs),
            "returned": len(items),
            "latest_seen": latest_seen,
            "repo_url": JOB_RADAR_REPO_URL,
            "jobs_url": JOB_RADAR_JOBS_URL,
            "health_url": JOB_RADAR_HEALTH_URL,
            "page_url": JOB_RADAR_PAGE_URL,
            "industries": industries,
            "health": {
                "generated_at": health.get("generated_at"),
                "sources_total": health.get("sources_total"),
                "jobs_raw": health.get("jobs_raw"),
                "snapshot_after_dedup": health.get("snapshot_after_dedup"),
                "store_total": health.get("store_total"),
                "new_this_run": health.get("new_this_run"),
                "active_total": health.get("active_total"),
                "alerts_count": len(alerts),
            },
        },
    }


@app.post("/api/opportunities/import")
def import_opportunities(body: OpportunityImportIn):
    _require_feature_available("autumn_opportunity_library")
    created = []
    skipped = []
    for item in body.items:
        clean = _clean_parsed_opportunity(item, {
            "source_type": item.get("source_type") or "manual",
            "source_title": item.get("source_title") or "",
            "source_url": item.get("source_url") or "",
        })
        existing = db.list_job_opportunities(q=clean["company"])
        duplicate = next((x for x in existing
                          if (x.get("company") or "").strip().lower() == clean["company"].strip().lower()
                          and (x.get("role") or "").strip().lower() == clean["role"].strip().lower()
                          and (x.get("deadline_date") or "") == (clean.get("deadline_date") or "")), None)
        if duplicate:
            skipped.append({"reason": "duplicate", "existing_id": duplicate["id"],
                            "company": clean["company"], "role": clean["role"]})
            continue
        oid = db.create_job_opportunity(clean)
        created.append(db.get_job_opportunity(oid))
    if created:
        _commit(f"导入秋招机会 {len(created)} 条")
    return {"items": created, "count": len(created), "skipped": skipped}


@app.get("/api/opportunities")
def list_opportunities(status: Optional[str] = None, batch_type: Optional[str] = None, q: Optional[str] = None):
    return {
        "items": db.list_job_opportunities(status=status, batch_type=batch_type, q=q),
        "status_labels": db.OPPORTUNITY_STATUS_LABEL,
        "batch_labels": db.OPPORTUNITY_BATCH_LABEL,
    }


@app.get("/api/opportunities/radar")
def opportunities_radar():
    return {
        "items": db.list_opportunity_radar(),
        "status_labels": db.OPPORTUNITY_STATUS_LABEL,
        "batch_labels": db.OPPORTUNITY_BATCH_LABEL,
    }


@app.post("/api/opportunities")
def add_opportunity(body: JobOpportunityIn):
    _require_feature_available("autumn_opportunity_library")
    oid = db.create_job_opportunity(_opportunity_payload(body))
    _commit(f"新增秋招机会：{body.company} · {body.role}")
    return {"id": oid, "opportunity": db.get_job_opportunity(oid)}


def _ics_text(value):
    text = str(value or "")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%Y%m%d")
    except Exception:
        return None


@app.get("/api/opportunities/calendar.ics")
def opportunity_calendar_ics(days: int = 30, backlog_days: int = 14):
    days = max(1, min(int(days or 30), 90))
    backlog_days = max(0, min(int(backlog_days or 14), 30))
    today = datetime.now().date()
    date_from = (today - timedelta(days=backlog_days)).strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=days + 1)).strftime("%Y-%m-%d")
    items = [x for x in db.list_opportunity_plan_schedule(date_from, date_to)
             if (x.get("status") or "todo") != "done"]
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Caddie//Autumn Recruiting Radar//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Caddie 秋招投递提醒",
    ]
    for item in items:
        start = _ics_date(item.get("due_date") or item.get("interview_date"))
        if not start:
            continue
        end_dt = datetime.strptime(start, "%Y%m%d") + timedelta(days=1)
        title = f"Caddie：{item.get('title') or item.get('round_type') or '处理秋招机会'}"
        company_role = " · ".join(x for x in [item.get("company"), item.get("role")] if x)
        desc = "\n".join(x for x in [
            company_role,
            item.get("notes"),
            f"投递链接：{item.get('apply_url')}" if item.get("apply_url") else "",
            "按自然日倒排：截止日 - 全流程天数 - 缓冲天数 = 最晚启动日。",
        ] if x)
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:caddie-opportunity-{item.get('opportunity_id')}-{item.get('id')}@caddie.local",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end_dt.strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_text(title)}",
            f"DESCRIPTION:{_ics_text(desc)}",
            "BEGIN:VALARM",
            "TRIGGER:-PT9H",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_ics_text(title)}",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines) + "\r\n"
    return Response(
        content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="caddie-autumn-recruiting.ics"'},
    )


@app.get("/api/opportunities/{oid}")
def get_opportunity(oid: int):
    item = db.get_job_opportunity(oid)
    if not item:
        raise HTTPException(404, "机会不存在")
    return item


@app.put("/api/opportunities/{oid}")
def edit_opportunity(oid: int, body: JobOpportunityIn):
    _require_feature_available("autumn_opportunity_library")
    ok = db.update_job_opportunity(oid, _opportunity_payload(body))
    if not ok:
        raise HTTPException(404, "机会不存在")
    _commit(f"编辑秋招机会：{body.company} · {body.role}")
    return {"ok": True, "opportunity": db.get_job_opportunity(oid)}


@app.put("/api/opportunities/{oid}/rules")
def edit_opportunity_rules(oid: int, body: OpportunityRuleIn):
    _require_feature_available("autumn_opportunity_library")
    ok = db.update_opportunity_rules(oid, body.dict())
    if not ok:
        raise HTTPException(404, "机会不存在")
    _commit("更新秋招机会规则卡")
    return {"ok": True, "opportunity": db.get_job_opportunity(oid)}


@app.post("/api/opportunities/{oid}/plan/rebuild")
def rebuild_opportunity_plan(oid: int, body: dict = None):
    _require_feature_available("autumn_opportunity_library")
    if not db.get_job_opportunity(oid):
        raise HTTPException(404, "机会不存在")
    items = db.rebuild_opportunity_plan(oid, force=True)
    _commit("重建秋招机会倒排计划")
    return {"items": items, "opportunity": db.get_job_opportunity(oid)}


@app.post("/api/opportunities/{oid}/plan-items")
def add_opportunity_plan_item(oid: int, body: OpportunityPlanItemIn):
    _require_feature_available("autumn_opportunity_library")
    if not db.get_job_opportunity(oid):
        raise HTTPException(404, "机会不存在")
    pid = db.create_opportunity_plan_item(oid, body.dict())
    _commit("新增秋招机会计划项")
    return {"id": pid}


@app.put("/api/opportunity-plan-items/{pid}")
def edit_opportunity_plan_item(pid: int, body: OpportunityPlanItemIn):
    _require_feature_available("autumn_opportunity_library")
    db.update_opportunity_plan_item(pid, body.dict())
    _commit("更新秋招机会计划项")
    return {"ok": True}


@app.post("/api/opportunities/{oid}/convert")
def convert_opportunity(oid: int, body: dict = None):
    _require_feature_available("autumn_opportunity_library")
    result = db.convert_opportunity_to_track(
        oid,
        create_application_record=bool((body or {}).get("create_application")),
    )
    if not result:
        raise HTTPException(404, "机会不存在")
    track = result.get("track") or {}
    _commit(f"秋招机会转入工作台：{track.get('company','')} · {track.get('role','')}")
    return result


@app.delete("/api/opportunities/{oid}")
def del_opportunity(oid: int):
    _require_feature_available("autumn_opportunity_library")
    item = db.get_job_opportunity(oid) or {}
    if not item or not db.delete_opportunity(oid):
        raise HTTPException(404, "机会不存在")
    _commit(f"删除秋招机会：{item.get('company','')} · {item.get('role','')}")
    return {"ok": True}


# ─── 投递 ─────────────────────────────────────────────────────────────────────

@app.get("/api/applications")
def list_applications():
    return {"items": db.get_applications(), "labels": db.STATUS_LABEL, "flow": db.STATUS_FLOW}


@app.get("/api/applications/{aid}")
def get_application(aid: int):
    item = db.get_application(aid)
    if not item:
        raise HTTPException(404, "投递不存在")
    return item


@app.post("/api/applications")
def add_application(body: ApplicationIn):
    try:
        aid = db.create_application(body.dict())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _commit(f"新增投递：{body.company} · {body.role}")
    return {"id": aid}


@app.put("/api/applications/{aid}")
def edit_application(aid: int, body: ApplicationIn):
    try:
        changed = db.update_application(aid, body.dict())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        raise HTTPException(404, "投递不存在")
    _commit(f"编辑投递：{body.company}")
    return {"ok": True}


@app.post("/api/applications/{aid}/status")
def move_application(aid: int, body: ApplicationStatusIn):
    try:
        changed = db.set_application_status(aid, body.status, allow_skip=bool(body.allow_skip))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not changed:
        raise HTTPException(404, "投递不存在")
    _commit(f"投递状态更新 → {db.STATUS_LABEL.get(body.status, body.status)}")
    return {"ok": True}


@app.post("/api/applications/{aid}/classify-company")
def classify_application_company(aid: int):
    a = db.classify_application(aid)
    if not a:
        raise HTTPException(404, "投递不存在")
    _commit(f"自动归类投递公司：{a.get('company') or ''}")
    return {"application": a}


@app.post("/api/applications/{aid}/track")
def link_application_track(aid: int, body: ApplicationTrackIn):
    try:
        changed = db.set_application_track(aid, body.track_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        raise HTTPException(404, "投递不存在")
    _commit("关联投递到求职目标" if body.track_id else "解除投递关联")
    return {"ok": True}


@app.delete("/api/applications/{aid}")
def del_application(aid: int):
    if not db.delete_application(aid):
        raise HTTPException(404, "投递不存在")
    _commit("删除一条投递")
    return {"ok": True}


@app.post("/api/applications/{aid}/interviews", deprecated=True)
def add_interview_schedule(aid: int, body: InterviewScheduleIn):
    app_ = db.get_application(aid)
    if not app_:
        raise HTTPException(404, "投递不存在")
    iid = db.add_interview(aid, body.dict())
    if app_.get("status") in ("applied", "screening", "written"):
        db.set_application_status(aid, "interview", allow_skip=True)
    _commit(f"安排面试：{app_.get('company','')} · {app_.get('role','')}")
    return {"id": iid}


@app.post("/api/job-tracks/{tid}/interviews")
def add_track_interview_schedule(tid: int, body: InterviewScheduleIn):
    """Create a calendar-visible interview through the track's canonical application."""
    application, application_created = db.ensure_track_application(
        tid,
        {"status": "interview", "source": "求职日历"},
    )
    if not application:
        raise HTTPException(404, "求职线不存在")
    iid = db.add_interview(application["id"], body.dict())
    if application.get("status") in ("applied", "screening", "written"):
        db.set_application_status(application["id"], "interview", allow_skip=True)
    _commit(f"从求职线安排面试：{application.get('company','')} · {application.get('role','')}")
    return {
        "id": iid,
        "application_id": application["id"],
        "track_id": tid,
        "application_created": application_created,
    }


@app.get("/api/applications/{aid}/timeline")
def get_application_timeline(aid: int):
    result = db.list_application_timeline(aid)
    if not result:
        raise HTTPException(404, "投递不存在")
    return result


@app.post("/api/applications/{aid}/milestones")
def add_application_milestone(aid: int, body: ApplicationMilestoneIn):
    try:
        mid = db.create_application_milestone(aid, body.dict())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _commit("新增投递进程节点")
    return {"id": mid}


@app.delete("/api/application-milestones/{mid}")
def remove_application_milestone(mid: int):
    if not db.delete_application_milestone(mid):
        raise HTTPException(404, "进程节点不存在")
    _commit("删除投递进程节点")
    return {"ok": True}


@app.put("/api/interviews/{iid}", deprecated=True)
def edit_interview_schedule(iid: int, body: InterviewScheduleIn):
    if not db.update_interview(iid, body.dict()):
        raise HTTPException(404, "面试安排不存在")
    _commit("更新面试安排")
    return {"ok": True}


@app.delete("/api/interviews/{iid}", deprecated=True)
def delete_interview_schedule(iid: int):
    if not db.delete_interview(iid):
        raise HTTPException(404, "面试安排不存在")
    _commit("删除面试安排")
    return {"ok": True}


@app.get("/api/calendar")
def calendar(date_from: Optional[str] = None, date_to: Optional[str] = None):
    items = db.list_interview_schedule(date_from, date_to)
    if product_features.get_feature("autumn_opportunity_library")["status"] != "available":
        items = [item for item in items if item.get("kind") != "opportunity_plan"]
    return {"items": items}


def _calendar_kind(item):
    if item.get("kind") == "career_calendar":
        return "career_calendar"
    if item.get("kind") == "opportunity_plan":
        return "opportunity_plan"
    if item.get("kind") == "application_applied":
        return "application_applied"
    if item.get("kind") == "application_milestone":
        return "application_milestone"
    return "interview"


def _ics_datetime(value):
    raw = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw[:19] if fmt.endswith("%S") else raw[:16] if "%H" in fmt else raw[:10], fmt)
            if "%H" not in fmt:
                parsed = parsed.replace(hour=9)
            return parsed
        except ValueError:
            continue
    return None


@app.get("/api/calendar/export.ics")
def export_caddie_calendar(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    item_kind: Optional[str] = None,
    item_id: Optional[int] = None,
):
    today = datetime.now().date()
    date_from = date_from or today.strftime("%Y-%m-%d")
    date_to = date_to or (today + timedelta(days=180)).strftime("%Y-%m-%d")
    if item_id is not None:
        # A wide read keeps the export endpoint independent of three source
        # tables while still selecting exactly one typed item below.
        items = db.list_interview_schedule("1970-01-01", "2100-01-01")
        items = [x for x in items if int(x.get("id") or 0) == item_id
                 and _calendar_kind(x) == (item_kind or "interview")]
    else:
        items = db.list_interview_schedule(date_from, date_to)
    if product_features.get_feature("autumn_opportunity_library")["status"] != "available":
        items = [item for item in items if item.get("kind") != "opportunity_plan"]
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Caddie//Career Calendar//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Caddie 求职计划",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for item in items:
        start = _ics_datetime(item.get("interview_date") or item.get("starts_at") or item.get("due_date"))
        if not start:
            continue
        duration = max(5, min(int(item.get("duration_minutes") or 30), 1440))
        end = start + timedelta(minutes=duration)
        kind = _calendar_kind(item)
        title = item.get("title") or item.get("round_type") or "求职安排"
        company_role = " · ".join(x for x in [item.get("company"), item.get("role")] if x)
        summary = f"{title}｜{company_role}" if company_role else title
        description = "\n".join(x for x in [
            company_role,
            item.get("notes"),
            f"目标：{item.get('target_count')} 份" if item.get("target_count") else "",
            "来自 Caddie 求职日历",
        ] if x)
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:caddie-{kind}-{item.get('id')}@caddie.local",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID=Asia/Shanghai:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Asia/Shanghai:{end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{_ics_text(summary)}",
            f"DESCRIPTION:{_ics_text(description)}",
            "BEGIN:VALARM",
            "TRIGGER:-PT15M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_ics_text(summary)}",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines) + "\r\n"
    filename = "Caddie-求职日历.ics" if item_id is None else "Caddie-求职安排.ics"
    return Response(
        content,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/calendar-items")
def add_calendar_item(body: CareerCalendarItemIn):
    try:
        iid = db.create_career_calendar_item(body.dict())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    _commit("新增岗位日程")
    return {"id": iid}


@app.put("/api/calendar-items/{iid}")
def edit_calendar_item(iid: int, body: CareerCalendarItemIn):
    if not db.update_career_calendar_item(iid, body.dict()):
        raise HTTPException(404, "日程不存在")
    _commit("更新岗位日程")
    return {"ok": True}


@app.delete("/api/calendar-items/{iid}")
def remove_calendar_item(iid: int):
    if not db.delete_career_calendar_item(iid):
        raise HTTPException(404, "日程不存在")
    _commit("删除岗位日程")
    return {"ok": True}


@app.get("/api/dashboard")
def dashboard():
    return db.dashboard_stats()


# ─── 检索 ─────────────────────────────────────────────────────────────────────

@app.get("/api/search")
def search(q: str = ""):
    if not q.strip():
        return []
    return db.search(q)


@app.get("/api/retrieval/index/health")
def retrieval_index_health():
    return db.search_index_health()


@app.post("/api/retrieval/index/rebuild")
def rebuild_retrieval_index():
    try:
        result = retrieval.sync_index()
    except Exception as exc:
        raise HTTPException(503, f"索引重建失败：{str(exc)[:240]}") from exc
    return {"ok": True, **result, "health": db.search_index_health()}


# ─── 会话 ─────────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
def get_sessions():
    return db.list_sessions()


@app.post("/api/sessions")
def new_session():
    sid = uuid.uuid4().hex
    db.create_session(sid)
    return {"id": sid}


@app.post("/api/workspace-sessions/resolve")
def resolve_workspace_session(body: WorkspaceSessionResolveIn):
    key = body.workspace_task_key.strip()
    if not key or len(key) > 240:
        raise HTTPException(400, "工作台任务身份无效")
    project_id = body.project_id or (body.target_id if body.target_type == "project" else None)
    # Legacy callers used experience_id for a project id. Convert once at the boundary.
    if body.mode == "experience" and not project_id and body.experience_type == "project":
        project_id = body.experience_id
    if body.mode == "experience":
        if not project_id or not db.get_project(project_id): raise HTTPException(404, "绑定项目不存在")
        expected_key = f"experience:project:{project_id}"
        if key != expected_key: raise HTTPException(409, "工作台任务身份与项目不一致")
    session = db.resolve_workspace_session(
        key, mode=body.mode, title=body.title.strip() or "Agent 工作台",
        track_id=body.track_id, knowledge_item_id=body.knowledge_item_id,
        gap_id=body.gap_id, experience_id=body.experience_id,
        experience_type=body.experience_type, target_type="project" if project_id else body.target_type,
        target_id=project_id or body.target_id, project_id=project_id,
    )
    if body.mode == "experience" and int(session.get("project_id") or session.get("target_id") or 0) != int(project_id):
        raise HTTPException(409, "已有任务绑定了其他项目")
    return {
        "session": session,
        "messages": db.get_chat_history(session["id"], limit=200),
        "agent_task": _agent_task_payload(
            db.latest_agent_task_for_conversation(session["id"])
        ),
        "track": db.get_job_track(session.get("track_id")) if session.get("track_id") else None,
        "knowledge_item": (
            _enrich_knowledge_item(db.get_knowledge_item(session.get("knowledge_item_id")))
            if session.get("knowledge_item_id") and db.get_knowledge_item(session.get("knowledge_item_id"))
            else None
        ),
        "experience": db.get_project(session.get("project_id") or session.get("target_id")) if session.get("workspace_mode") == "experience" else None,
        "experience_state": db.get_experience_workspace_state(session["id"]),
        "experience_candidates": db.list_experience_candidates(session["id"]) if session.get("workspace_mode") == "experience" else [],
    }


@app.post("/api/workspace-sessions/{sid}/messages")
def append_workspace_message(sid: str, body: WorkspaceMessageIn):
    if not db.get_session(sid):
        raise HTTPException(404, "工作台任务不存在")
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "消息内容不能为空")
    message_id = db.save_message(sid, body.role, content)
    db.touch_session(sid)
    return {"id": message_id}


EXPERIENCE_INTENTS = {"discuss_experience","inspect_experience","add_fact","correct_fact","identify_evidence_gap","generate_followup_questions","improve_expression","adapt_to_job","create_resume_version","simulate_interview","record_interview_feedback"}
EXPERIENCE_EXPERTS = {"experience_detective","job_researcher","resume_editor","knowledge_coach","pressure_interviewer","review_analyst"}


def _experience_list(value, limit=8):
    """Normalize loose model output into a bounded list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value[:limit]
    if isinstance(value, tuple):
        return list(value[:limit])
    if isinstance(value, dict):
        return [value]
    return [value]


def _normalize_experience_question_plan(raw):
    """Keep persisted/UI question plans stable across model output variants."""
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    normalized = []
    for item in items[:12]:
        if isinstance(item, str):
            question, reason = item.strip(), ""
        elif isinstance(item, dict):
            question = str(item.get("question") or item.get("text") or item.get("content") or item.get("prompt") or item.get("title") or "").strip()
            reason = str(item.get("reason") or item.get("rationale") or item.get("purpose") or "").strip()
        else:
            continue
        if question:
            raw_options = item.get("options") if isinstance(item, dict) else []
            options = []
            for option in raw_options if isinstance(raw_options, list) else []:
                if isinstance(option, str):
                    label, description = option.strip(), ""
                elif isinstance(option, dict):
                    label = str(option.get("label") or option.get("title") or option.get("value") or "").strip()
                    description = str(option.get("description") or option.get("detail") or option.get("reason") or "").strip()
                else:
                    continue
                if label:
                    options.append({"label": label[:200], "description": description[:500]})
            normalized.append({"question": question[:1000], "reason": reason[:1000], "options": options[:4]})
    return normalized


def _reconcile_experience_reply(reply, has_failed_candidates=False):
    text = str(reply or "").strip()
    if not has_failed_candidates:
        return text
    lines = [line for line in text.splitlines() if not ("点击" in line and "写入" in line)]
    body = "\n".join(lines).strip()
    notice = "本轮内容仍在补充口径阶段，尚未进入可确认写入状态。请在右栏选择“拆解数据来源”“补充已有信息”或“先用定性表达”继续。"
    return notice + (("\n\n" + body) if body else "")


def _normalize_experience_dispatch(raw, session, project, requested_expert=None):
    data = raw if isinstance(raw, dict) else {}
    raw_intent = data.get("intent")
    intent = raw_intent if isinstance(raw_intent, str) and raw_intent in EXPERIENCE_INTENTS else "discuss_experience"
    defaults = {
        "inspect_experience": ("experience_detective", []), "identify_evidence_gap": ("experience_detective", []),
        "generate_followup_questions": ("experience_detective", ["pressure_interviewer"]), "add_fact": ("experience_detective", []),
        "correct_fact": ("experience_detective", []), "improve_expression": ("resume_editor", ["experience_detective"]),
        "adapt_to_job": ("job_researcher", ["resume_editor"]), "create_resume_version": ("resume_editor", []),
        "simulate_interview": ("pressure_interviewer", []), "record_interview_feedback": ("review_analyst", ["experience_detective"]),
        "discuss_experience": ("experience_detective", []),
    }
    primary, supporting = defaults[intent]
    plan = data.get("expert_plan") or {}
    if not isinstance(plan, dict): plan = {}
    if requested_expert in EXPERIENCE_EXPERTS: primary = requested_expert
    elif plan.get("primary") in EXPERIENCE_EXPERTS: primary = plan["primary"]
    supporting = [x for x in _experience_list(plan.get("supporting") or supporting, 3) if x in EXPERIENCE_EXPERTS and x != primary][:3]
    if "knowledge_coach" in supporting and intent not in {"adapt_to_job"}: supporting.remove("knowledge_coach")
    needs = bool(data.get("needs_clarification"))
    try: confidence=max(0,min(1,float(data.get("confidence") or .5)))
    except (TypeError,ValueError): confidence=.5
    return {"intent":intent,"target":{"type":"project","target_type":"project","target_id":project["id"],"project_id":project["id"],"title":project.get("name")},"references":_experience_list(data.get("references")),"operations":_experience_list(data.get("operations")),"affected_fields":_experience_list(data.get("affected_fields")),"needs_clarification":needs,"confidence":confidence,"evidence":[str(x)[:500] for x in _experience_list(data.get("evidence"))],"confirmation_policy":"confirm_before_write","expert_plan":{"primary":primary,"supporting":supporting},"clarification_question":str(data.get("clarification_question") or "") if needs else None}


@app.post("/api/workspace-sessions/{sid}/experience-dispatch")
def dispatch_experience_input(sid: str, body: ExperienceDispatchIn):
    session = db.get_session(sid)
    if not session or session.get("workspace_mode") != "experience": raise HTTPException(404, "经历任务不存在")
    project = db.get_project(session.get("project_id") or session.get("target_id"))
    if not project: raise HTTPException(404, "绑定经历不存在")
    context = {"bound_experience":{"id":project["id"],"title":project.get("name"),"one_liner":project.get("one_liner"),"document":project.get("document")},"recent_messages":db.get_chat_history(sid,limit=12),"user_input":body.message}
    prompt = """你是 Caddie 经历任务分诊器，只返回 JSON。intent 必须属于 discuss_experience,inspect_experience,add_fact,correct_fact,identify_evidence_gap,generate_followup_questions,improve_expression,adapt_to_job,create_resume_version,simulate_interview,record_interview_feedback。输出 operations,affected_fields,needs_clarification,confidence,evidence,expert_plan(primary,supporting),clarification_question。question_plan 必须是对象数组，每项严格使用 {question,reason,options}；options 提供 2-4 个互斥选项，每项严格使用 {label,description}，label 是可直接选择的短答案，description 解释适用情况。确实只能自由输入时 options 才可为空。不得编造事实或数字；不确定内容进入追问；修改必须先生成候选。"""
    try:
        selection=_expert_model_selection(db.get_agent_expert("experience_detective") or {})
        raw=ai.chat([{"role":"user","content":json.dumps(context,ensure_ascii=False)}],system=prompt,max_tokens=1000,provider=selection["provider"])
        parsed=ai.extract_json(raw)
    except Exception: parsed={"intent":"discuss_experience","evidence":["分诊服务不可用，已降级为讨论模式"],"confidence":0.3}
    if not isinstance(parsed, dict):
        parsed={"intent":"discuss_experience","evidence":["分诊返回格式异常，已降级为讨论模式"],"confidence":0.3}
    dispatch=_normalize_experience_dispatch(parsed,session,project,body.requested_expert)
    questions=_normalize_experience_question_plan(parsed.get("question_plan"))
    db.save_experience_workspace_state(sid,project["id"],dispatch,questions,dispatch["expert_plan"]["primary"])
    return {"dispatch":dispatch,"question_plan":questions}


@app.get("/api/workspace-sessions/{sid}/experience-candidates")
def experience_candidates(sid: str):
    session=db.get_session(sid)
    if not session or session.get("workspace_mode")!="experience": raise HTTPException(404,"经历任务不存在")
    return {"items":db.list_experience_candidates(sid)}


def _experience_evidence_text(evidence):
    return " ".join(str(x.get("content") or x.get("quote") or "") for x in evidence if isinstance(x,dict))


def _experience_explicit_change_request(message, dispatch):
    intent=(dispatch or {}).get("intent")
    if intent not in {"add_fact","correct_fact","improve_expression","adapt_to_job"}:
        return False
    text=str(message or "")
    return any(token in text for token in ("补充","添加","加上","加一个","修改","改为","改成","写成","替换","删除"))


def _experience_requested_field(dispatch):
    for item in _experience_list((dispatch or {}).get("affected_fields")):
        field=item if isinstance(item,str) else item.get("field") if isinstance(item,dict) else None
        if field in db.EXPERIENCE_PROJECT_FIELDS:
            return field
    return "document"


def _validate_experience_candidate(project, field, proposed, evidence, evidence_source, unverified, trusted_user_text=""):
    if field not in db.EXPERIENCE_PROJECT_FIELDS: raise HTTPException(400,"目标字段不在白名单")
    original=str(project.get(field) or ""); evidence_text=_experience_evidence_text(evidence)
    unsupported_numbers=[x for x in re.findall(r"\d+(?:\.\d+)?%?",proposed) if x not in (evidence_text+original)]
    if unsupported_numbers: raise HTTPException(422,"待补充数据口径："+"、".join(unsupported_numbers)+"。请拆解来源、估算公式和最小输入，再生成可确认候选")
    if unverified: raise HTTPException(422,"待补充事实口径：请把未确认内容拆成可回答的问题或可核验来源")
    if evidence_source in {"material","user_statement"} and not evidence_text.strip(): raise HTTPException(422,"待补充依据：请说明数据来源、计算方式或用户确认的事实")
    if evidence_source=="user_statement" and any(str(x.get("content") or "").strip() not in trusted_user_text for x in evidence if isinstance(x,dict)):
        raise HTTPException(422,"用户陈述依据不属于当前会话")
    material_text=" ".join(str(project.get(k) or "") for k in db.EXPERIENCE_PROJECT_FIELDS)
    if evidence_source=="material" and any(str(x.get("content") or "").strip() not in material_text for x in evidence if isinstance(x,dict)):
        raise HTTPException(422,"材料依据不属于当前绑定项目")
    if evidence_source=="wording_only":
        # Wording-only may rearrange existing facts but cannot introduce new factual atoms.
        factual=re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}|[一-鿿]{2,}",proposed)
        baseline=original+" "+str(project.get("document") or "")+" "+str(project.get("technologies") or "")
        risky_verbs=("负责","独立","主导","设计","开发","上线","提升","降低","实现","覆盖","支持")
        introduced=[x for x in factual if len(x)>=2 and x not in baseline and any(v in x or v in proposed for v in risky_verbs)]
        if introduced: raise HTTPException(422,"待确认表达口径：候选加入了原文未明确支持的职责、结果、技术或范围；请让用户确认事实、补充来源，或改为原文可支持的定性表达")
    return original


@app.post("/api/workspace-sessions/{sid}/experience-candidates")
def create_experience_candidate(sid: str, body: ExperienceCandidateIn):
    session=db.get_session(sid)
    if not session or session.get("workspace_mode")!="experience": raise HTTPException(404,"经历任务不存在")
    bound_project_id=session.get("project_id") or session.get("target_id")
    if body.target_id != bound_project_id: raise HTTPException(409,"目标项目与当前任务不一致")
    project=db.get_project(bound_project_id)
    if not project: raise HTTPException(404,"经历不存在")
    trusted_user_text="\n".join(x.get("content") or "" for x in db.get_chat_history(sid,limit=200) if x.get("role")=="user")
    original=_validate_experience_candidate(project,body.field,body.proposed_value,body.evidence,body.evidence_source,body.unverified_claims,trusted_user_text)
    if body.original_value is not None and body.original_value != original: raise HTTPException(409,"原值已发生变化")
    cid=db.create_experience_candidate({**body.dict(),"session_id":sid,"project_id":bound_project_id,"original_value":original})
    return {"candidate_id":cid,"status":"pending_confirmation"}


@app.patch("/api/workspace-sessions/{sid}/experience-candidates/{candidate_id}")
def revise_experience_candidate(sid:str,candidate_id:int,body:ExperienceCandidateReviseIn):
    session=db.get_session(sid); items={x["id"]:x for x in db.list_experience_candidates(sid)}
    if not session or candidate_id not in items: raise HTTPException(404,"候选不存在")
    item=items[candidate_id]; project=db.get_project(session.get("project_id") or session.get("target_id"))
    trusted_user_text="\n".join(x.get("content") or "" for x in db.get_chat_history(sid,limit=200) if x.get("role")=="user")
    _validate_experience_candidate(project,item["field"],body.proposed_value,body.evidence,body.evidence_source,body.unverified_claims,trusted_user_text)
    db.revise_experience_candidate(sid,candidate_id,body.proposed_value,body.evidence,body.evidence_source,body.unverified_claims)
    return {"item":next(x for x in db.list_experience_candidates(sid) if x["id"]==candidate_id)}


@app.post("/api/workspace-sessions/{sid}/experience-candidates/{candidate_id}/reject")
def reject_experience_candidate(sid:str,candidate_id:int):
    if not db.set_experience_candidate_status(sid,candidate_id,"rejected"): raise HTTPException(404,"待确认候选不存在")
    return {"ok":True}


@app.post("/api/workspace-sessions/{sid}/experience-candidates/{candidate_id}/retry")
def retry_experience_candidate(sid:str,candidate_id:int):
    items={x["id"]:x for x in db.list_experience_candidates(sid)}; item=items.get(candidate_id)
    if not item or item.get("status")!="failed": raise HTTPException(409,"只有失败候选可以重试")
    session=db.get_session(sid)
    if not session or session.get("workspace_mode")!="experience": raise HTTPException(404,"经历任务不存在")
    project=db.get_project(session.get("project_id") or session.get("target_id"))
    if not project or int(item.get("project_id") or item.get("target_id")) != int(project["id"]):
        raise HTTPException(409,"失败候选与当前绑定项目不一致")
    history=db.get_chat_history(sid,limit=40)
    retry_system="""你正在重试一个验证失败的经历修改候选。只返回一个 JSON 对象，字段必须是 field, proposed_value,
evidence_source(material/user_statement/wording_only), evidence, unverified_claims。不得改变目标字段。
必须修正失败原因；没有依据时只能忠实改写已有事实，不能新增数字、职责、结果、技术或范围。"""
    retry_context={"bound_project":{k:project.get(k) for k in ("id","name","one_liner","document","technologies","keywords","experience")},
                   "failed_candidate":{k:item.get(k) for k in ("field","proposed_value","evidence_source","evidence","failure_reason")},
                   "user_statements":[x.get("content") for x in history if x.get("role")=="user"]}
    try:
        selection=ai.resolve_model_profile("writing")
        raw=ai.chat([{"role":"user","content":json.dumps(retry_context,ensure_ascii=False)}],system=retry_system,
                    max_tokens=1000,provider=selection["provider"],allow_fallback=True)
        candidate=ai.extract_json(raw)
        if not isinstance(candidate,dict): raise HTTPException(422,"模型未返回结构化候选")
        if candidate.get("field") != item.get("field"): raise HTTPException(422,"重试不得改变目标字段")
        proposed=str(candidate.get("proposed_value") or "")
        evidence_source=candidate.get("evidence_source") or "wording_only"
        evidence=candidate.get("evidence") or []; unverified=candidate.get("unverified_claims") or []
        trusted_user_text="\n".join(str(x.get("content") or "") for x in history if x.get("role")=="user")
        _validate_experience_candidate(project,item["field"],proposed,evidence,evidence_source,unverified,trusted_user_text)
        db.revise_experience_candidate(sid,candidate_id,proposed,evidence,evidence_source,unverified)
    except HTTPException as exc:
        candidate=locals().get("candidate") if isinstance(locals().get("candidate"),dict) else {}
        db.fail_experience_candidate(sid,candidate_id,str(candidate.get("proposed_value") or item.get("proposed_value") or ""),
            candidate.get("evidence") or [],candidate.get("evidence_source") or "wording_only",
            candidate.get("unverified_claims") or [],str(exc.detail))
    except Exception as exc:
        db.fail_experience_candidate(sid,candidate_id,item.get("proposed_value") or "",item.get("evidence") or [],
            item.get("evidence_source") or "wording_only",item.get("unverified_claims") or [],f"重试模型调用失败：{str(exc)[:200]}")
    return {"item":next(x for x in db.list_experience_candidates(sid) if x["id"]==candidate_id)}


@app.post("/api/workspace-sessions/{sid}/experience-candidates/apply")
def apply_experience_candidates(sid: str, body: ExperienceCandidateApplyIn):
    session=db.get_session(sid)
    if not session or session.get("workspace_mode")!="experience": raise HTTPException(404,"经历任务不存在")
    if not body.candidate_ids: raise HTTPException(400,"请选择候选")
    try: results=db.apply_experience_candidates(sid,body.candidate_ids)
    except RuntimeError as exc: raise HTTPException(409,str(exc))
    except ValueError as exc: raise HTTPException(400,str(exc))
    return {"ok":True,"results":results}


@app.post("/api/workspace-sessions/{sid}/knowledge-topic")
def confirm_workspace_knowledge_topic(sid: str, body: KnowledgeTopicIn):
    session = db.get_session(sid)
    if not session or session.get("workspace_mode") != "knowledge_coach":
        raise HTTPException(404, "知识任务不存在")
    topic = re.sub(r"\s+", " ", body.topic).strip()
    title = topic if session.get("track_id") else f"通用知识 · {topic}"
    title = title[:28]
    db.update_session(sid, title=title)
    return {"ok": True, "title": title, "session": db.get_session(sid)}


KNOWLEDGE_DISPATCH_INTENTS = {
    "discuss", "clarify_topic", "create_document", "create_multiple_documents",
    "expand_document", "edit_document", "split_document", "merge_documents",
    "continue_document", "change_document_purpose", "retry_failed_document",
}


def _normalize_knowledge_dispatch(raw, session, current_task):
    data = raw if isinstance(raw, dict) else {}
    intent = data.get("intent") if data.get("intent") in KNOWLEDGE_DISPATCH_INTENTS else "clarify_topic"
    topics = [str(x).strip()[:120] for x in (data.get("resolved_topics") or []) if str(x).strip()][:8]
    plan = []
    for item in (data.get("document_plan") or [])[:8]:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        source_ids = []
        for value in item.get("source_candidate_ids") or []:
            try: source_ids.append(int(value))
            except (TypeError, ValueError): pass
        plan.append({
            "title": str(item.get("title"))[:120], "goal": str(item.get("goal") or "")[:1000],
            "action": item.get("action") if item.get("action") in {"create", "revise_candidate", "update_document"} else "create",
            "source_candidate_ids": source_ids[:8],
            "target_knowledge_item_id": item.get("target_knowledge_item_id"),
        })
    needs = bool(data.get("needs_clarification")) or intent == "clarify_topic"
    if intent not in {"discuss", "clarify_topic", "continue_document", "retry_failed_document"} and not plan:
        needs = True
    confidence = max(0.0, min(1.0, float(data.get("confidence") or 0)))
    if confidence < .62:
        needs = True
    return {
        "intent": intent, "references": (data.get("references") or [])[:8],
        "resolved_topics": topics, "operations": (data.get("operations") or [])[:8],
        "target_scope": {"type": "current_job" if session.get("track_id") else "global", "track_id": session.get("track_id")},
        "output": data.get("output") or {"type": "knowledge_document", "mode": "one_document", "count": len(plan)},
        "document_plan": plan, "needs_clarification": needs, "confidence": confidence,
        "evidence": [str(x)[:500] for x in (data.get("evidence") or [])[:8]],
        "confirmation_policy": "create_as_candidates",
        "technical_failure": bool(data.get("technical_failure")),
        "clarification_question": str(data.get("clarification_question") or "请说明要处理的具体主题或文档。") if needs else None,
        "source_task_id": current_task.get("id") if current_task else None,
    }


def _knowledge_dispatch_failure_kind(exc: Exception) -> str:
    detail = str(exc or "").lower()
    if "timeout" in detail or "timed out" in detail or "超时" in detail:
        return "timeout"
    if any(token in detail for token in ("connection", "network", "httpsconnectionpool", "连接")):
        return "provider_unavailable"
    if isinstance(exc, (ValueError, json.JSONDecodeError)) or "json" in detail:
        return "invalid_structured_output"
    return "dispatch_runtime_error"


def _resolve_dispatch_candidate(plan_item: dict, candidates: list[dict], topics: list[str]) -> dict | None:
    plan_title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(plan_item.get("title") or "").lower())
    scored = []
    for candidate in candidates:
        candidate_title = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(candidate.get("proposed_title") or "").lower())
        score = 0
        if plan_title and candidate_title and (plan_title in candidate_title or candidate_title in plan_title):
            score += 20
        for topic in topics:
            token = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(topic or "").lower())
            if token and token in plan_title and token in candidate_title:
                score += 10
        if score:
            scored.append((score, int(candidate.get("id") or 0), candidate))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return scored[0][2]


@app.post("/api/workspace-sessions/{sid}/knowledge-dispatch")
def dispatch_workspace_knowledge_input(sid: str, body: KnowledgeDispatchIn):
    session = db.get_session(sid)
    if not session or session.get("workspace_mode") != "knowledge_coach":
        raise HTTPException(404, "知识任务不存在")
    history = db.get_chat_history(sid, limit=16)
    current_task = db.latest_agent_task_for_conversation(sid)
    current_payload = _agent_task_payload(db.get_agent_task(current_task["id"])) if current_task else None
    conversation_tasks = db.list_agent_tasks_for_conversation(sid, limit=10)
    conversation_candidates = []
    seen_candidate_ids = set()
    for prior_task in conversation_tasks:
        for candidate in prior_task.get("changes") or []:
            if candidate.get("id") in seen_candidate_ids or candidate.get("status") not in {"planned", "generating", "pending", "failed"}:
                continue
            seen_candidate_ids.add(candidate.get("id")); conversation_candidates.append(candidate)
    track = db.get_job_track(session.get("track_id")) if session.get("track_id") else None
    item = db.get_knowledge_item(session.get("knowledge_item_id")) if session.get("knowledge_item_id") else None
    context = {
        "current_job": ({"track_id": track["id"], "company": track.get("company"), "role": track.get("role") or track.get("target")} if track else None),
        "session": {"id": sid, "title": session.get("title"), "workspace_task_key": session.get("workspace_task_key")},
        "recent_messages": [{"role": x.get("role"), "content": (x.get("content") or "")[:6000]} for x in history[-12:]],
        "current_topics": (_json_loads_safe((current_task or {}).get("context_json"), {}) or {}).get("dispatch", {}).get("resolved_topics", []),
        "candidates": [{"id": x.get("id"), "task_id": x.get("task_id"), "title": x.get("proposed_title"), "status": x.get("status"), "action_type": x.get("action_type"), "content_length": len(x.get("proposed_content") or "")} for x in conversation_candidates],
        "bound_document": ({"id": item["id"], "title": item.get("title"), "scope_type": item.get("scope_type")} if item else None),
        "user_input": body.message,
    }
    prompt = """你是 Caddie 知识教练的输入分诊器。结合全部上下文解析指代和真实操作，不要因为 topic 为空就机械追问。
只返回 JSON。intent 必须是 discuss, clarify_topic, create_document, create_multiple_documents, expand_document, edit_document, split_document, merge_documents, continue_document, change_document_purpose, retry_failed_document 之一。
document_plan 每项：title, goal, action(create/revise_candidate/update_document), source_candidate_ids, target_knowledge_item_id。
明确且只创建候选时直接执行；只有指代多解、目标文档不明、要求冲突或置信度低时 needs_clarification=true。
当用户要求加深、调整、合并当前待确认候选时，必须优先返回 revise_candidate 并绑定 candidates 中的 ID；只有用户明确要改已写入的正式文档时才使用 update_document。
同一主题存在多个候选时，优先最新 task_id 中、ID 最大的待确认候选，不得要求用户自己选技术 ID。
输出字段：intent,references,resolved_topics,operations,target_scope,output,document_plan,needs_clarification,confidence,evidence,clarification_question。"""
    selection = _expert_model_selection(db.get_agent_expert("knowledge_coach") or {})
    failure_task_id = None
    try:
        raw = ai.chat([{"role": "user", "content": json.dumps(context, ensure_ascii=False)}], system=prompt, max_tokens=1800, provider=selection["provider"])
        try:
            parsed = ai.extract_json(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            repair_prompt = """将下面的知识任务分诊结果修复为一个完整 JSON 对象。只输出 JSON，不得改变原意。
必须包含 intent,references,resolved_topics,operations,target_scope,output,document_plan,needs_clarification,confidence,evidence,clarification_question。
document_plan 每项必须包含 title,goal,action,source_candidate_ids,target_knowledge_item_id。"""
            repaired = ai.chat([{"role": "user", "content": raw[:12000]}], system=repair_prompt, max_tokens=1800, provider=selection["provider"])
            parsed = ai.extract_json(repaired)
        dispatch = _normalize_knowledge_dispatch(parsed, session, current_task or {})
    except Exception as exc:
        failure_kind = _knowledge_dispatch_failure_kind(exc)
        failure_task_id = db.create_agent_task({
            "task_type": "knowledge_dispatch", "title": "知识任务分诊待恢复",
            "instruction": body.message, "object_type": "knowledge_item" if session.get("knowledge_item_id") else ("job_track" if session.get("track_id") else None),
            "object_id": session.get("knowledge_item_id") or session.get("track_id"), "track_id": session.get("track_id"),
            "assigned_expert": "knowledge_coach", "conversation_id": sid, "status": "failed",
            "result_summary": "分诊服务本轮未完成，可使用原输入重试",
            "context_json": json.dumps({"mode": "knowledge_coach", "failure_kind": failure_kind, "retry_message": body.message, "input_context": context, "technical_detail": str(exc)[:1000]}, ensure_ascii=False),
        })
        db.create_agent_event({"task_id": failure_task_id, "event_type": "dispatch_failed", "label": "分诊未完成", "detail": failure_kind, "status": "failed"})
        dispatch = _normalize_knowledge_dispatch({
            "intent": "clarify_topic", "needs_clarification": True, "confidence": 0,
            "clarification_question": "这次输入已收到，但分诊服务暂时没有完成解析。请重试本条消息，无需重新说明主题。",
            "evidence": ["分诊服务本轮未完成"],
        }, session, current_task or {})
        dispatch["technical_failure"] = True
        dispatch["failure_kind"] = failure_kind
        dispatch["failure_task_id"] = failure_task_id
    candidate_ids = {x.get("id") for x in conversation_candidates}
    validation_questions = []
    recovered_candidate_target = False
    for operation in dispatch.get("document_plan") or []:
        if operation.get("action") == "revise_candidate" and (
            not operation.get("source_candidate_ids")
            or any(cid not in candidate_ids for cid in operation.get("source_candidate_ids") or [])
        ):
            validation_questions.append("当前没有找到与修改目标匹配的待确认候选。")
        target_id = operation.get("target_knowledge_item_id")
        if operation.get("action") == "update_document" and (not target_id or not db.get_knowledge_item(target_id)):
            matched = _resolve_dispatch_candidate(operation, conversation_candidates, dispatch.get("resolved_topics") or [])
            if matched:
                operation["action"] = "revise_candidate"
                operation["source_candidate_ids"] = [matched["id"]]
                operation["target_knowledge_item_id"] = None
                recovered_candidate_target = True
                dispatch["evidence"].append(f"已自动定位待确认候选《{matched.get('proposed_title') or '未命名文档'}》")
            else:
                validation_questions.append("当前没有找到与该主题匹配的候选或正式文档。")
    if validation_questions:
        dispatch["needs_clarification"] = True
        dispatch["clarification_question"] = validation_questions[0]
    elif recovered_candidate_target:
        dispatch["needs_clarification"] = False
        dispatch["clarification_question"] = None
    if dispatch.get("intent") == "continue_document" and not dispatch.get("needs_clarification"):
        resumable = next((task for task in conversation_tasks if any(
            change.get("status") in {"planned", "generating", "pending", "failed"}
            for change in (task.get("changes") or [])
        )), None)
        if resumable:
            comparable = [change for change in (resumable.get("changes") or []) if change.get("status") in {"pending", "applied"} and (change.get("proposed_content") or "").strip()]
            benchmark_length = max((len(change.get("proposed_content") or "") for change in comparable), default=0)
            shallow_ids = [
                change["id"] for change in comparable if change.get("status") == "pending"
                and benchmark_length >= 1000 and len(change.get("proposed_content") or "") < max(800, int(benchmark_length * .55))
            ]
            for change_id in shallow_ids:
                db.update_proposed_change(change_id, "failed")
                db.create_agent_event({
                    "task_id": resumable["id"], "event_type": "quality_retry",
                    "label": "候选深度不足，纳入续跑", "detail": "以同组最完整文档为质量基准", "status": "pending",
                })
            if shallow_ids:
                resumable = db.get_agent_task(resumable["id"])
            failed_ids = [change["id"] for change in (resumable.get("changes") or []) if change.get("status") == "failed"]
            pending_count = len([change for change in (resumable.get("changes") or []) if change.get("status") == "pending"])
            return {
                "dispatch": dispatch, "task": None, "resume_task_id": resumable["id"],
                "retry_change_ids": failed_ids, "pending_count": pending_count,
                "already_ready": not failed_ids,
            }
    if dispatch["needs_clarification"] or dispatch["intent"] == "discuss":
        return {"dispatch": dispatch, "task": None, "retry_available": bool(failure_task_id)}
    if dispatch["intent"] == "retry_failed_document":
        return {"dispatch": dispatch, "task": None, "retry_task_id": dispatch.get("source_task_id")}
    title = "、".join(dispatch["resolved_topics"][:3]) or (dispatch["document_plan"][0]["title"] if dispatch["document_plan"] else "知识任务")
    task_id = db.create_agent_task({
        "task_type": "knowledge_orchestrated", "title": title[:120], "instruction": body.message,
        "object_type": "knowledge_item" if session.get("knowledge_item_id") else ("job_track" if session.get("track_id") else None),
        "object_id": session.get("knowledge_item_id") or session.get("track_id"), "track_id": session.get("track_id"),
        "assigned_expert": "knowledge_coach", "conversation_id": sid, "status": "queued",
        "context_json": json.dumps({"mode": "knowledge_coach", "dispatch": dispatch, "input_context": context}, ensure_ascii=False),
    })
    db.create_agent_event({"task_id": task_id, "event_type": "dispatch", "label": f"已识别意图：{dispatch['intent']}", "detail": "；".join(dispatch["evidence"][:3]), "status": "done", "payload_json": json.dumps(dispatch, ensure_ascii=False)})
    if title:
        db.update_session(sid, title=title[:28])
    return {"dispatch": dispatch, "task": _agent_task_payload(db.get_agent_task(task_id))}


@app.post("/api/workspace-sessions/{sid}/knowledge-dispatch/retry")
def retry_workspace_knowledge_dispatch(sid: str, body: KnowledgeDispatchRetryIn):
    task = db.get_agent_task(body.failure_task_id)
    if not task or task.get("task_type") != "knowledge_dispatch" or task.get("conversation_id") != sid:
        raise HTTPException(404, "可恢复的分诊任务不存在")
    if task.get("status") != "failed":
        raise HTTPException(409, "该分诊任务无需重试")
    result = dispatch_workspace_knowledge_input(sid, KnowledgeDispatchIn(message=task.get("instruction") or ""))
    if not result.get("dispatch", {}).get("technical_failure"):
        db.update_agent_task(task["id"], status="completed", result_summary="分诊已恢复")
        db.create_agent_event({"task_id": task["id"], "event_type": "dispatch_recovered", "label": "分诊已恢复", "detail": "已复用原输入和任务上下文", "status": "done"})
    return result


@app.get("/api/sessions/{sid}/messages")
def session_messages(sid: str):
    return db.get_chat_history(sid, limit=200)


@app.get("/api/chat/history", deprecated=True)
def chat_history_legacy(session_id: str):
    if not db.get_session(session_id):
        raise HTTPException(404, "任务不存在")
    return db.get_chat_history(session_id, limit=200)


@app.patch("/api/sessions/{sid}")
def update_session(sid: str, body: SessionUpdateIn):
    if body.title is None and body.folder is None and body.is_pinned is None:
        raise HTTPException(400, "没有需要修改的内容")
    if not db.update_session(sid, title=body.title, folder=body.folder, is_pinned=body.is_pinned):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@app.put("/api/sessions/{sid}/workspace")
def update_session_workspace(sid: str, body: dict):
    if not db.get_chat_history(sid, limit=1) and not any(
        item.get("id") == sid for item in db.list_sessions()
    ):
        raise HTTPException(404, "任务不存在")
    run_id = body.get("run_id")
    if run_id is not None and not db.get_discovery_run(int(run_id)):
        raise HTTPException(404, "本地工作区不存在")
    db.set_session_workspace(sid, int(run_id) if run_id is not None else None)
    return {"ok": True, "run_id": run_id}


CHAT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
CHAT_DOCUMENT_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}


@app.post("/api/chat/attachments")
async def upload_chat_attachment(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "附件是空文件")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "单个附件不能超过 20MB")
    name = (file.filename or "附件").replace("/", "_")
    mime = (file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream").lower()
    suffix = Path(name).suffix.lower()
    is_image = mime in CHAT_IMAGE_TYPES
    if not is_image and suffix not in CHAT_DOCUMENT_SUFFIXES:
        raise HTTPException(400, "支持 PNG/JPG/WebP/GIF、PDF、Word、TXT、Markdown、CSV 和 JSON")
    extracted = ""
    if not is_image:
        try:
            extracted = _extract_text(raw, name).strip()
        except Exception as exc:
            raise HTTPException(400, f"文件解析失败：{str(exc)[:160]}")
        if not extracted:
            raise HTTPException(400, "没有从附件中读到文字；扫描版 PDF 请先截图后作为图片上传")
        extracted = extracted[:60000]
    store_dir = ai.CONFIG_DIR / "chat_uploads"
    store_dir.mkdir(exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}-{name}"
    path = store_dir / stored_name
    path.write_bytes(raw)
    aid = db.create_chat_attachment({
        "file_name": name, "mime_type": mime, "file_path": str(path),
        "size_bytes": len(raw), "extracted_text": extracted,
    })
    return {"id": aid, "file_name": name, "mime_type": mime,
            "size_bytes": len(raw), "is_image": is_image,
            "preview_url": f"/api/chat/attachments/{aid}/preview" if is_image else None}


@app.get("/api/chat/attachments/{aid}/preview")
def preview_chat_attachment(aid: int):
    items = db.get_chat_attachments([aid])
    if not items or not (items[0].get("mime_type") or "").startswith("image/"):
        raise HTTPException(404, "图片不存在")
    path = Path(items[0]["file_path"])
    if not path.exists():
        raise HTTPException(404, "图片文件不存在")
    return FileResponse(path, media_type=items[0]["mime_type"], filename=items[0]["file_name"])


@app.delete("/api/chat/attachments/{aid}")
def delete_chat_attachment(aid: int):
    path = db.delete_unbound_chat_attachment(aid)
    if not path:
        raise HTTPException(409, "已发送的附件不能从历史记录中单独删除")
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


COMPANY_LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def _company_logo_key(company: str) -> str:
    normalized = re.sub(r"\s+", "", (company or "").strip().lower())
    if not normalized:
        raise HTTPException(400, "公司名称不能为空")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _company_logo_match_name(company: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (company or "").strip().lower())


def _find_company_logo_meta(company: str) -> tuple[Path, dict] | tuple[None, None]:
    store_dir = ai.CONFIG_DIR / "company_logos"
    exact = store_dir / f"{_company_logo_key(company)}.json"
    if exact.exists():
        try:
            return exact, json.loads(exact.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if not store_dir.exists():
        return None, None
    wanted = _company_logo_match_name(company)
    matches = []
    for meta_path in store_dir.glob("*.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            saved = _company_logo_match_name(meta.get("company") or "")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if min(len(wanted), len(saved)) >= 2 and (wanted.startswith(saved) or saved.startswith(wanted)):
            matches.append((min(len(wanted), len(saved)), meta_path, meta))
    if not matches:
        return None, None
    _, meta_path, meta = max(matches, key=lambda item: item[0])
    return meta_path, meta


def _copy_company_logo(old_company: str, new_company: str) -> None:
    if _company_logo_key(old_company) == _company_logo_key(new_company):
        return
    store_dir = ai.CONFIG_DIR / "company_logos"
    new_meta_path = store_dir / f"{_company_logo_key(new_company)}.json"
    if new_meta_path.exists():
        return
    _, meta = _find_company_logo_meta(old_company)
    if not meta:
        return
    source = store_dir / str(meta.get("file_name") or "")
    if not source.exists() or source.parent != store_dir:
        return
    suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".webp"} else ".png"
    target = store_dir / f"{_company_logo_key(new_company)}{suffix}"
    target.write_bytes(source.read_bytes())
    new_meta_path.write_text(json.dumps({
        **meta, "company": new_company.strip(), "file_name": target.name,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")


@app.post("/api/company-logos/{company}")
async def upload_company_logo(company: str, file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "图片是空文件")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(400, "Logo 图片不能超过 5MB")
    mime = (file.content_type or "").lower()
    suffix = COMPANY_LOGO_TYPES.get(mime)
    if not suffix:
        raise HTTPException(400, "支持 PNG、JPG 和 WebP 图片")
    key = _company_logo_key(company)
    store_dir = ai.CONFIG_DIR / "company_logos"
    store_dir.mkdir(parents=True, exist_ok=True)
    for old in store_dir.glob(key + ".*"):
        old.unlink(missing_ok=True)
    path = store_dir / f"{key}{suffix}"
    path.write_bytes(raw)
    (store_dir / f"{key}.json").write_text(json.dumps({
        "company": company.strip(), "mime_type": mime, "file_name": path.name,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "url": f"/api/company-logos/{quote(company)}?v={int(time.time())}"}


@app.get("/api/company-logos/{company}")
def get_company_logo(company: str):
    store_dir = ai.CONFIG_DIR / "company_logos"
    _, meta = _find_company_logo_meta(company)
    if not meta:
        raise HTTPException(404, "还没有手动上传 Logo")
    try:
        path = store_dir / meta["file_name"]
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        raise HTTPException(404, "Logo 记录不可用")
    if not path.exists() or path.parent != store_dir:
        raise HTTPException(404, "Logo 文件不存在")
    return FileResponse(path, media_type=meta.get("mime_type") or "image/png")


@app.delete("/api/sessions/{sid}")
def remove_session(sid: str):
    db.delete_session(sid)
    return {"ok": True}


# ─── 对话 ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是 Caddie，用户的本地求职管家。你的职责：
1. 帮用户深挖、记录项目经历，把模糊的描述追问成可写进简历的细节（背景-行动-量化结果）。
2. 基于用户已有的经历和项目回答问题，帮他准备面试。
3. 分析面试反馈，指出答得薄弱的地方，给出更好的回答思路。

重要规则：Caddie 支持“候选修改 → 用户确认 → 系统写入”的两阶段操作。
你不能绕过用户确认静默改库，也不能在候选尚未确认时声称“已更新看板/已更新文档/已保存”。
当对话里出现可以入库的信息（新项目细节、新投递、面试反馈、状态变化）时，
应明确告诉用户：“我可以生成结构化候选修改；你确认后，系统会真正写入。”
当用户明确说“整理入库 / 写入 / 保存上面的内容”时，不要再次强调没有权限，也不要要求重复粘贴；
应承接当前完整回答生成候选修改，并提示用户点击“写入职业档案”，再在右侧确认。
面向用户只能使用公司、岗位、项目名称和自然语言动作；不得展示数据库 id、字段名或
merge_to_existing、primary_source_id 等内部指令。内部编号只允许存在于系统提交数据中。
用中文回答，专业、直接、不啰嗦。"""

CHAT_MODE_PROMPTS = {
    "general": "和用户一起分析问题。先给判断，再给依据和下一步，不要复述全部背景。",
    "plan": "把问题转成可执行计划。区分现在要做、稍后要做和暂时不做，并明确每一步的产物。",
    "review": "以复盘教练的视角工作。识别事实、表现、原因和可迁移改进，避免泛泛鼓励。",
}


def _experience_context() -> str:
    exps = db.get_experiences()
    if not exps:
        return "\n\n（用户还没有录入任何经历。）"
    s = "\n\n【用户已有经历概览——这是你的跨对话记忆，任何会话都基于它】\n"
    for e in exps:
        s += f"- {e['company']} | {e['role']}（{e.get('start_date','')}~{e.get('end_date','至今')}）\n"
        for p in e.get("projects", []):
            s += f"    · 项目：{p['name']} — {p.get('one_liner') or ''}\n"
    return s


def _load_chat_local_context(candidate_ids: list[int], total_limit: int = 24000, per_file_limit: int = 6000):
    ids = []
    for raw in candidate_ids or []:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid > 0 and cid not in ids:
            ids.append(cid)
    ids = ids[:20]
    parts, refs, issues = [], [], []
    seen_content = set()
    used = 0
    for cid in ids:
        item = db.get_file_candidate(cid)
        if not item:
            issues.append(f"候选文件 #{cid} 不存在")
            continue
        path = Path(item.get("file_path") or "").expanduser()
        title = item.get("title") or item.get("file_name") or path.name or f"本地文件 #{cid}"
        if not path.exists() or not path.is_file():
            issues.append(f"{title} 的本地文件已不存在")
            continue
        try:
            text = _extract_text(path.read_bytes(), path.name).strip()
        except Exception as exc:
            text = (item.get("sample_text") or "").strip()
            issues.append(f"{title} 读取完整内容失败，已使用扫描摘要：{str(exc)[:80]}")
        if not text:
            issues.append(f"{title} 没有可读取文字")
            continue
        content_key = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        if content_key in seen_content:
            issues.append(f"{title} 与本轮已读取文件内容重复，已跳过副本")
            continue
        seen_content.add(content_key)
        remain = total_limit - used
        if remain <= 800:
            issues.append("本轮本地资料过多，后续文件未继续读取")
            break
        clip = text[:min(per_file_limit, remain)]
        used += len(clip)
        parts.append(
            f"### 本地文件：{title}\n"
            f"候选ID：{cid}\n"
            f"路径：{path}\n"
            f"识别类型：{item.get('source_type') or 'other'}\n"
            f"内容摘录：\n{clip}"
        )
        refs.append({
            "type": "local_file",
            "id": cid,
            "label": "本地文件",
            "title": title,
            "reason": "本轮临时挂载，未入库",
            "path": str(path),
        })
    if issues:
        parts.append("### 本地文件读取提示\n" + "\n".join(f"- {x}" for x in issues[:10]))
    return "\n\n".join(parts), refs, issues


def _local_query_terms(query: str) -> set[str]:
    text = (query or "").lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,8}", text))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    terms.update(chinese[i:i + 2] for i in range(max(0, len(chinese) - 1)))
    return {term for term in terms if term.strip()}


def _select_workspace_candidates(run_id: int, query: str, limit: int = 8) -> list[int]:
    run = db.get_discovery_run(run_id)
    if not run:
        raise HTTPException(400, "绑定的本地工作区已经失效，请重新选择文件夹")
    candidates = [
        item for item in db.list_file_candidates(run_id=run_id, limit=1000)
        if item.get("status") not in {"ignored", "error"}
    ]
    terms = _local_query_terms(query)
    ranked = []
    for item in candidates:
        title = item.get("title") or item.get("file_name") or ""
        haystack = " ".join(filter(None, [
            title, item.get("summary"), item.get("sample_text"), item.get("file_path"),
        ])).lower()
        score = float(item.get("confidence") or 0)
        for term in terms:
            if term in title.lower():
                score += 5
            elif term in haystack:
                score += 1
        ranked.append((score, item.get("modified_at") or "", int(item["id"])))
    ranked.sort(reverse=True)
    return [item_id for _, _, item_id in ranked[:limit]]


def _chat_confirmation_candidates(reply: str) -> list[dict]:
    """Turn an assistant's explicit request for confirmation into a real action.

    This is deliberately conservative: ordinary advice must never become a write
    candidate.  A candidate is produced only when the answer explicitly asks to
    add a numbered Q&A to a project's interview-preparation question bank and a
    single existing project can be resolved from the answer text.
    """
    text = str(reply or "").strip()
    if not text or "确认" not in text:
        return []
    requests_write = bool(re.search(r"(?:加入|写入|放入)", text))
    requests_followup = requests_write and bool(re.search(r"(?:面试准备|追问|题库)", text))
    requests_profile = requests_write and bool(re.search(
        r"(?:职业档案|项目档案|经历档案|写入档案|项目经历|项目结构|项目更新|"
        r"项目写入|完整的?\s*[A-Za-z\u4e00-\u9fff·\s]{0,30}项目)",
        text,
    ))
    combined_update = requests_write and bool(re.search(
        r"(?:两处|多处|一并|同时|项目描述|项目概述|候选修改|完整.*项目)", text,
    ))
    if requests_profile or combined_update:
        return [{
            "type": "propose_profile_write",
            "label": "查看并确认这次档案更新",
            "description": "系统会列出项目正文和面试准备的具体改动；点击后直接生成写入预览，不需要在聊天框回复“确认”。",
        }]
    if not requests_followup:
        return []
    question_match = re.search(
        r"(?ms)^\s*(?:#{1,6}\s*)?(Q\d+)\s*[：:]\s*(.+?)(?=\n\s*(?:#{1,6}\s+|(?:先给结论|参考回答|建议答法|回答|A\d*|边界声明)\s*[：:]?|Q\d+\s*[：:])|\Z)",
        text,
    )
    if not question_match:
        return []
    question = re.sub(r"\s+", " ", question_match.group(2)).strip(" -*")
    if len(question) < 4:
        return []

    projects = db.list_projects()
    normalized_text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.lower())
    matches = []
    for project in projects:
        name = str(project.get("name") or "").strip()
        normalized_name = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name.lower())
        tokens = [x for x in re.split(r"[·•：:—\-_/（）()\s]+", name) if len(x) >= 2]
        exact = bool(normalized_name and normalized_name in normalized_text)
        token_hits = sum(1 for token in tokens if re.sub(r"\W+", "", token.lower()) in normalized_text)
        if exact or token_hits >= 1:
            matches.append((2 if exact else 1, token_hits, len(normalized_name), project))
    if not matches:
        return []
    matches.sort(key=lambda item: (item[0], item[1], item[2], item[3].get("updated_at") or ""), reverse=True)
    best = matches[0]
    if len(matches) > 1 and best[:3] == matches[1][:3]:
        return []
    project = best[3]

    answer_match = re.search(
        r"(?ms)^\s*(?:#{1,6}\s*)?(?:先给结论|参考回答|建议答法|回答|A\d*)\s*[：:]?\s*(.+?)(?=\n\s*(?:#{1,6}\s+|边界声明\s*[：:]?|Q\d+\s*[：:])|\Z)",
        text[question_match.end():],
    )
    answer = (answer_match.group(1).strip() if answer_match else "")
    return [{
        "type": "add_project_followup",
        "project_id": project["id"],
        "project_name": project.get("name") or "当前项目",
        "question": question[:300],
        "answer": answer[:2000],
        "status": "todo",
        "category": "项目判断",
        "origin": "chat_confirmation",
        "label": f"将 {question_match.group(1)} 加入面试准备",
    }]


@app.post("/api/chat")
def chat(body: ChatIn):
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位不存在")
    session_id = body.session_id or uuid.uuid4().hex
    db.create_session(session_id)
    session = db.get_session(session_id) or {}
    experience_mode = session.get("workspace_mode") == "experience"
    bound_project = None
    experience_dispatch_result = None
    if experience_mode:
        bound_project_id = session.get("project_id") or session.get("target_id")
        if session.get("target_type") != "project" or not bound_project_id:
            raise HTTPException(409, "经历工作台没有可信项目绑定")
        if body.target_type and body.target_type != "project": raise HTTPException(409, "前端目标类型与服务端绑定冲突")
        if body.target_id and int(body.target_id) != int(bound_project_id): raise HTTPException(409, "前端目标与服务端绑定冲突")
        if body.workspace_mode and body.workspace_mode != "experience": raise HTTPException(409, "工作台模式与服务端绑定冲突")
        bound_project = db.get_project(bound_project_id)
        if not bound_project: raise HTTPException(404, "绑定项目不存在")
        experience_dispatch_result = dispatch_experience_input(
            session_id, ExperienceDispatchIn(message=body.message, requested_expert=body.expert_key)
        )
    history = db.get_chat_history(session_id, limit=16)
    effective_track_id = body.track_id
    scope_auto_detected = False
    if not effective_track_id:
        history_text = "\n".join(
            str(item.get("content") or "") for item in history[-8:]
        )
        effective_track_id = caddie_context.infer_track_scope(
            body.message, history_text
        )
        scope_auto_detected = bool(effective_track_id)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    attachments = db.get_chat_attachments(body.attachment_ids)
    if len(attachments) != len(set(body.attachment_ids)):
        raise HTTPException(400, "部分附件已失效，请重新添加")
    for item in attachments:
        if item.get("message_id") is not None and item.get("session_id") != body.session_id:
            raise HTTPException(400, "附件已属于另一段对话")
    image_items = [x for x in attachments if (x.get("mime_type") or "").startswith("image/")]
    local_context_ids = list(body.local_context_ids)
    if body.local_workspace_run_id:
        local_context_ids = _select_workspace_candidates(
            body.local_workspace_run_id, body.message, limit=8
        )
    local_context_text, local_refs, local_issues = _load_chat_local_context(local_context_ids)
    user_text = body.message or ("请基于本轮本地资料回答。" if local_context_text else "请分析附件。")
    if local_context_text:
        user_text = f"{user_text}\n\n【本轮临时本地资料】\n{local_context_text}"
    content_parts = [{"type": "text", "text": user_text}]
    for item in attachments:
        if item in image_items:
            try:
                encoded = base64.b64encode(Path(item["file_path"]).read_bytes()).decode("ascii")
            except OSError:
                raise HTTPException(400, f"附件文件已丢失：{item['file_name']}")
            content_parts.append({"type": "image", "media_type": item["mime_type"], "data": encoded})
        else:
            content_parts.append({"type": "text", "text":
                f"\n\n【本轮附件：{item['file_name']}】\n{item.get('extracted_text') or ''}"})
    messages.append({"role": "user", "content": content_parts if attachments else user_text})
    if experience_mode:
        project_context = {
            "project_id": bound_project["id"], "name": bound_project.get("name"),
            "one_liner": bound_project.get("one_liner"), "document": bound_project.get("document"),
            "technologies": bound_project.get("technologies"), "keywords": bound_project.get("keywords"),
            "parent_experience": bound_project.get("experience") or {},
        }
        context_data = {
            "text": "【服务端可信绑定项目；默认禁止读取其他经历】\n" + json.dumps(project_context, ensure_ascii=False, indent=2),
            "refs": [{"type":"project","id":bound_project["id"],"title":bound_project.get("name")}],
        }
        effective_track_id = None
    else:
        context_data = caddie_context.build_context(track_id=effective_track_id, intent=f"global_chat:{body.mode}", query=body.message)
    scope_rule = ("当前是服务端绑定的单一项目经历对话。只能使用该项目、所属经历的必要背景，以及用户本轮明确添加的资料；不得读取同经历其他项目。"
        if experience_mode else (
            "当前对话绑定了一个具体岗位。回答要优先服务这个岗位，同时说明哪些结论可以跨岗位复用。"
            if effective_track_id else
            "当前是全局职业对话。不要假装绑定某个岗位；需要岗位级判断时，明确建议用户选择岗位。"
        ))
    if scope_auto_detected:
        scope_rule += "\n系统已根据公司、岗位或内部文档名称自动识别工作范围；应直接使用已加载资料，不要要求用户重新粘贴。"
    scope_rule += {
        "concise": "\n本轮使用简洁模式：直接回答，通常不超过 3 句话；用户指定了更严格格式时优先遵守。",
        "standard": "\n本轮使用标准模式：先给结论，再给必要依据；不要为了结构完整而制造冗长段落。",
        "deep": "\n本轮使用深度模式：可以系统展开，但仍应遵守用户明确要求的输出格式。",
    }[body.response_style]
    if any(term in (body.message or "") for term in ("收件箱", "刚上传", "刚导入", "新增资料", "最近资料")):
        scope_rule += (
            "\n用户正在引用 Caddie 收件箱。上下文中的“最近收件箱资料”就是可读取的真实内容；"
            "先盘点资料和建议归属，不要声称没有收到，也不要要求用户重复粘贴。"
        )
    if experience_mode:
        dispatch = experience_dispatch_result["dispatch"]
        expert_key = dispatch["expert_plan"]["primary"]
        selected_expert = db.get_agent_expert(expert_key)
        route = {"expert_key":expert_key,"expert":selected_expert,"reason":"经历工作台统一分诊","confidence":dispatch["confidence"],
                 "task_type":agent_runtime.TASK_TYPES.get(expert_key,"general"),
                 "delegates":[db.get_agent_expert(x) for x in dispatch["expert_plan"].get("supporting",[]) if db.get_agent_expert(x)],
                 "scope":{"object_type":"project","object_id":bound_project["id"],"track_id":None}}
    else:
        route = agent_runtime.coordinate(body.message, object_type="job_track" if effective_track_id else "career_profile",
                                         object_id=effective_track_id, track_id=effective_track_id)
    if body.expert_key and not experience_mode:
        selected_expert = db.get_agent_expert(body.expert_key)
        if not selected_expert:
            raise HTTPException(400, "指定的专家不存在或已停用")
        route = {
            "expert_key": body.expert_key,
            "expert": selected_expert,
            "reason": "本轮由用户手动指定主理专家",
            "confidence": 1.0,
            "task_type": agent_runtime.TASK_TYPES.get(body.expert_key, "general"),
            "delegates": [],
            "scope": {
                "object_type": "job_track" if effective_track_id else "career_profile",
                "object_id": effective_track_id,
                "track_id": effective_track_id,
            },
        }
    chat_request_report = harness.validate_chat_request(
        body.message, route,
        attachment_count=len(attachments),
        local_context_count=len(local_refs),
        track_id=effective_track_id,
        mode=body.mode,
    )
    if not chat_request_report.passed:
        raise HTTPException(400, "Harness 拦截本轮对话：" + "；".join(
            x.message for x in chat_request_report.issues if x.level == "error"
        ))
    context_budget = {"concise": 7000, "standard": 12000, "deep": 18000}[body.response_style]
    packed_context = harness.pack_context(context_data["text"], {
        "id": session_id,
        "task_type": f"chat:{body.mode}",
        "assigned_expert": route["expert_key"],
        "object_type": "project" if experience_mode else ("job_track" if effective_track_id else "career_profile"),
        "object_id": bound_project["id"] if experience_mode else effective_track_id,
        "track_id": effective_track_id,
    }, budget=context_budget)
    chat_contract = harness.build_chat_contract(
        route, mode=body.mode, track_id=effective_track_id,
        local_context_count=len(local_refs), attachment_count=len(attachments),
        packed_context=packed_context,
    )
    system_parts = [
        SYSTEM_PROMPT,
        agent_runtime.system_prompt_for(route),
        chat_contract,
        CHAT_MODE_PROMPTS.get(body.mode, CHAT_MODE_PROMPTS["general"]),
        scope_rule,
        packed_context["text"],
    ]
    if not experience_mode:
        system_parts.insert(-1, _experience_context())
    system = "\n\n".join(system_parts)
    if experience_mode:
        system += """\n\n你正在服务服务端绑定的单一项目。你的身份就是本轮分诊指定的主理专家，不得再次分诊或切换专家。默认禁止读取其他经历。
只返回 JSON：reply 为回答；question_plan 为更新后的追问计划（reply 每轮最多问两个问题）；change_candidates 为候选数组。
question_plan 每项严格使用 {question,reason,options}；可选择的问题必须给 2-4 个 {label,description}，label 是短答案，description 解释适用情况；仅确实需要自由陈述时 options 才可为空。
用户希望加入暂时缺少直接证据的数字、职责或结果时，不要只说“不可以”。先帮助拆解：可能的数据来源、可解释的估算公式、需要用户补充的最小输入、以及没有数据时可用的定性替代表述；将这些路径放入带说明的 options。可以灵活表达，但必须明确哪些是原始事实、合理估算和表达优化。未经用户确认的估算仍不得写入正式项目。
不要在 reply 中宣称候选已通过校验、已经写入或可以直接写入，也不要编造或引用页面按钮名称。候选的真实状态由后端校验决定；reply 只说明内容和推导依据。
用户明确要求修改时必须生成 change_candidates，即使依据不足也不得只在 reply 中拒绝；将未核验内容写入 unverified_claims，交给服务端持久化为“未写入”候选。候选字段：candidate_id（调整现有候选时必须原样返回）, field, proposed_value, evidence_source(material/user_statement/wording_only), evidence, unverified_claims。
field 只能是 name,one_liner,document,technologies,keywords。evidence 每项包含 source 和 content。wording_only 只能重组原文，不得新增职责、结果、技术、范围或数字；未确认内容不得进入 proposed_value。"""
    # Response depth is a user-visible contract. Expert routing decides the
    # perspective, while the selected depth decides model cost and latency.
    requested_profile = "vision" if image_items else {
        "concise": "writing",
        "standard": "writing",
        "deep": "deep_reasoning",
    }[body.response_style]
    try:
        model_selection = ai.resolve_model_profile(requested_profile)
    except ai.AIError as exc:
        if image_items:
            raise HTTPException(400, "图片需要视觉模型。请先在「设置 → 能力分工」为视觉理解绑定可用模型。")
        raise HTTPException(400, str(exc))

    try:
        response_tokens = {"concise": 700, "standard": 1800, "deep": 3200}[body.response_style]
        response_timeout = {"concise": 20, "standard": 35, "deep": 60}[body.response_style]
        raw_reply = ai.chat(
            messages, system=system, max_tokens=response_tokens,
            provider=model_selection["provider"], timeout=response_timeout,
            # Reliability applies to every response style. The fallback chain
            # already preserves the configured quality order.
            allow_fallback=True,
        )
        if experience_mode:
            try:
                envelope = ai.extract_json(raw_reply)
            except (ValueError, TypeError):
                envelope = {"reply": str(raw_reply or "").strip(), "question_plan": experience_dispatch_result.get("question_plan") or [], "change_candidates": []}
            if not isinstance(envelope, dict) or not isinstance(envelope.get("reply"), str):
                envelope = {"reply": raw_reply, "question_plan": experience_dispatch_result.get("question_plan") or [], "change_candidates": []}
            reply = envelope["reply"]
        else:
            reply = raw_reply
    except ai.AIError as e:
        if image_items:
            raise HTTPException(400,
                f"当前视觉模型无法读取图片：{str(e)} 请在「设置 → 能力分工 → 图像理解」绑定真正支持图片的模型（如 GLM-4.6V-Flash）。")
        if body.response_style in {"standard", "deep"}:
            raise HTTPException(
                503,
                f"{str(e)} 当前是{'深度' if body.response_style == 'deep' else '标准'}模式，"
                "为避免无感降低回答质量，Caddie 没有切换到其他模型。"
                "你可以重试，或切换到「快速」模式生成初稿。"
            )
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:300]}")
    chat_audit_report = harness.audit_chat_answer(
        reply, route, mode=body.mode,
        local_context_count=len(local_refs), attachment_count=len(attachments),
        response_style=body.response_style,
    )
    # Auditing remains visible in local metadata, but it must not trigger a
    # second invisible model call after the user has already waited once.

    saved_user_text = body.message or ("请基于本轮本地资料回答。" if local_context_text else "请分析附件。")
    user_message_id = db.save_message(session_id, "user", saved_user_text)
    db.bind_message_local_files(user_message_id, local_context_ids)
    db.bind_chat_attachments(body.attachment_ids, session_id, user_message_id)
    questions = None
    processed_candidate_ids = []
    if experience_mode:
        questions = _normalize_experience_question_plan(envelope.get("question_plan") or experience_dispatch_result.get("question_plan"))
        db.save_experience_workspace_state(session_id, bound_project["id"], experience_dispatch_result["dispatch"], questions, route["expert_key"])
        candidate_payloads=[x for x in _experience_list(envelope.get("change_candidates")) if isinstance(x,dict)]
        for candidate in candidate_payloads:
            if not isinstance(candidate, dict): continue
            field = candidate.get("field"); proposed = str(candidate.get("proposed_value") or "")
            evidence_source = candidate.get("evidence_source") or "wording_only"
            evidence = _experience_list(candidate.get("evidence"),16); unverified = _experience_list(candidate.get("unverified_claims"),16)
            try:
                trusted_user_text = "\n".join([body.message] + [x.get("content") or "" for x in history if x.get("role")=="user"])
                original = _validate_experience_candidate(bound_project, field, proposed, evidence, evidence_source, unverified, trusted_user_text)
                existing_items = db.list_experience_candidates(session_id)
                existing_ids = {x["id"] for x in existing_items}
                candidate_id = candidate.get("candidate_id")
                if candidate_id not in existing_ids:
                    same_field = [x for x in existing_items if x.get("field") == field and x.get("status") == "failed"]
                    candidate_id = same_field[-1]["id"] if same_field else None
                if candidate_id:
                    db.revise_experience_candidate(session_id,candidate_id,proposed,evidence,evidence_source,unverified)
                else:
                    candidate_id = db.create_experience_candidate({"session_id":session_id,"project_id":bound_project["id"],"field":field,
                        "original_value":original,"proposed_value":proposed,"evidence":evidence,"evidence_source":evidence_source,
                        "unverified_claims":unverified,"question_plan":questions})
                processed_candidate_ids.append(candidate_id)
            except HTTPException as exc:
                existing_items = db.list_experience_candidates(session_id)
                existing_ids = {x["id"] for x in existing_items}
                candidate_id = candidate.get("candidate_id")
                if candidate_id not in existing_ids:
                    same_field = [x for x in existing_items if x.get("field") == field and x.get("status") == "failed"]
                    candidate_id = same_field[-1]["id"] if same_field else None
                if candidate_id:
                    current = next(x for x in existing_items if x["id"] == candidate_id)
                    if current.get("status") == "pending_confirmation":
                        db.set_experience_candidate_status(session_id,candidate_id,"failed",str(exc.detail))
                    db.fail_experience_candidate(session_id,candidate_id,proposed,evidence,evidence_source,unverified,str(exc.detail))
                else:
                    original = str(bound_project.get(field) or "") if field in db.EXPERIENCE_PROJECT_FIELDS else ""
                    candidate_id = db.create_experience_candidate({"session_id":session_id,"project_id":bound_project["id"],"field":str(field or "unknown"),
                        "original_value":original,"proposed_value":proposed,"evidence":evidence,"evidence_source":evidence_source,
                        "unverified_claims":unverified,"question_plan":questions,"status":"failed","failure_reason":str(exc.detail)})
                processed_candidate_ids.append(candidate_id)
        if not candidate_payloads and _experience_explicit_change_request(body.message,experience_dispatch_result.get("dispatch")):
            field=_experience_requested_field(experience_dispatch_result.get("dispatch"))
            proposed=str(body.message or "").strip()
            existing=db.list_experience_candidates(session_id)
            if not any(x.get("proposed_value")==proposed and x.get("status") in {"failed","pending_confirmation"} for x in existing):
                candidate_id=db.create_experience_candidate({"session_id":session_id,"project_id":bound_project["id"],"field":field,
                    "original_value":str(bound_project.get(field) or ""),"proposed_value":proposed,"evidence":[],
                    "evidence_source":"user_statement","unverified_claims":[proposed],"question_plan":questions,
                    "status":"failed","failure_reason":"用户明确要求修改，但当前没有足够的可核验依据；已保留为待核验请求，未写入项目。"})
                processed_candidate_ids.append(candidate_id)
        current_candidates = {x["id"]:x for x in db.list_experience_candidates(session_id)}
        has_failed_candidates = any(current_candidates.get(cid,{}).get("status") == "failed" for cid in processed_candidate_ids)
        reply = _reconcile_experience_reply(reply, has_failed_candidates)
    confirmations = _chat_confirmation_candidates(reply)
    assistant_message_id = db.save_message(session_id, "assistant", reply)
    db.touch_session(session_id, title=body.message)
    if body.local_workspace_run_id:
        db.set_session_workspace(session_id, body.local_workspace_run_id)
    actual_model = ai.get_last_call_info() or {}
    return {"reply": reply, "session_id": session_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "sources": (_assistant_sources(context_data) + local_refs)[:30],
            "scope": {"track_id": effective_track_id, "mode": body.mode,
                      "auto_detected": scope_auto_detected,
                      "workspace_mode":"experience" if experience_mode else session.get("workspace_mode"),
                      "target_type":"project" if experience_mode else None,
                      "target_id":bound_project["id"] if experience_mode else None},
            "dispatch": experience_dispatch_result.get("dispatch") if experience_mode else None,
            "question_plan": questions,
            "change_candidates": db.list_experience_candidates(session_id) if experience_mode else [],
            "confirmations": confirmations,
            "response_style": body.response_style,
            "harness": {
                "request": chat_request_report.to_dict(),
                "context": {
                    "budget": packed_context["budget"],
                    "used": packed_context["used"],
                    "truncated": packed_context["truncated"],
                    "refs": len(context_data.get("refs") or []),
                    "local_refs": len(local_refs),
                    "local_issues": local_issues[:8],
                },
                "output": chat_audit_report.to_dict(),
            },
            "expert": {
                "key": route["expert_key"],
                "name": route["expert"].get("name"),
                "role": route["expert"].get("role"),
                "icon": route["expert"].get("icon"),
                "color": route["expert"].get("color"),
                "model_profile": model_selection["profile_key"],
                "provider_name": actual_model.get("provider_name") or model_selection["provider_name"],
                "model": actual_model.get("model") or model_selection["model"],
                "fallback_used": bool(actual_model.get("fallback_position")),
                "reason": route["reason"],
                "confidence": route["confidence"],
                "delegates": [{"key": item.get("key"), "name": item.get("name")}
                              for item in route.get("delegates") or []],
            }}


@app.post("/api/chat/confirmations/resolve")
def resolve_chat_confirmations(body: ChatConfirmationResolveIn):
    """Resolve older assistant text into non-writing confirmation candidates."""
    return {"items": _chat_confirmation_candidates(body.content)}


@app.delete("/api/sessions/{sid}/messages/from/{message_id}")
def truncate_session_chat(sid: str, message_id: int):
    if not db.truncate_chat(sid, message_id):
        raise HTTPException(404, "消息不存在或不属于当前对话")
    return {"ok": True}


CHAT_ACTION_PROMPT = """你是 Caddie 的行动提取器。根据对话判断有哪些信息值得进入职业资料库。
只提取对话中已经明确出现、对以后确实有用的内容，不要把普通建议全部变成任务。

允许的 action_type：
- create_knowledge：可复用的知识或完整分析。字段 title, topic, reason；如果最后一条回答包含多份独立的 Markdown 代码块，必须增加 source_block_index（从 0 开始），指出这份知识对应第几个代码块。
- create_gap：当前岗位仍需补齐的能力、证据或准备项。字段 requirement, dimension, severity(blocker/fixable/nice), reason。只有绑定岗位时可用。
- save_feedback：以后生成内容必须遵守的事实修正或表达偏好。字段 directive, category(fact_correction/ownership/tone/emphasis/avoidance/interviewer_probe), strength(hard/soft), reason。

硬规则：
1. 不要在 JSON 里复制长回答正文；create_knowledge 保存时系统会引用最后一条 AI 回答或 source_block_index 指向的文档块。
2. 最多 5 个操作，宁缺毋滥；没有值得写入的内容就返回空数组。
3. title、requirement、directive 都必须能脱离当前对话独立看懂。
4. 绑定岗位：%s。

对话：
%s

只返回 JSON：
{"summary":"一句话", "actions":[{"action_type":"...","reason":"..."}]}
"""


def _fallback_chat_actions(history: list[dict], track_id=None) -> dict:
    """Turn the latest answer into a deterministic knowledge-action candidate."""
    latest_answer = next((
        (item.get("content") or "").strip() for item in reversed(history)
        if item.get("role") == "assistant" and (item.get("content") or "").strip()
    ), "")
    if len(latest_answer) < 80:
        return {"summary": "当前回答信息较少，暂时没有形成可安全写入的内容", "actions": []}
    track = db.get_job_track(track_id) if track_id else None
    role = (track or {}).get("role") or (track or {}).get("target") or ""
    title = f"{role}面试复盘与改进" if role else "对话复盘与改进"
    return {
        "summary": "已将本轮回答整理为知识候选；经历、项目或投递请使用“写入职业档案”",
        "actions": [{
            "action_type": "create_knowledge",
            "title": title[:100],
            "topic": "面试复盘",
            "reason": "保留本轮完整回答，确认后写入知识库",
            "track_id": track_id,
        }],
    }


def _non_scoring_requirement_reason(requirement: str | None) -> str | None:
    text = re.sub(r"\s+", "", requirement or "").lower()
    groups = (
        (("base", "工作地点", "办公地点", "地点", "坐标", "城市"), "地点偏好不属于能力证据"),
        (("到岗", "入职", "截止", "实习时长", "每周", "天/周", "个月"), "时间与到岗条件不属于能力证据"),
        (("薪资", "薪酬", "福利", "补贴", "投递邮箱", "投递方式"), "招聘流程或待遇信息不属于能力证据"),
        (("mentor", "带教", "团队氛围", "决策权", "听取实习生", "工作氛围"), "团队与管理方式不属于能力证据"),
    )
    for keywords, reason in groups:
        if any(keyword in text for keyword in keywords):
            return reason
    return None


def _fallback_gap_diagnosis(jd: str, background: str) -> dict:
    """Build a usable baseline when the provider cannot return valid JSON."""
    clean_jd = re.sub(r"[🏠💰📌📮]", " ", jd or "")
    clean_jd = re.sub(r"【投递邮箱】.*$", "", clean_jd, flags=re.S)
    for marker in ("【主要职责】", "【岗位职责】", "【工作内容】", "【岗位描述】"):
        if marker in clean_jd:
            clean_jd = clean_jd.split(marker, 1)[1]
            break
    clean_jd = clean_jd.replace("【任职要求】", "\n任职要求：")
    raw_lines = re.split(
        r"[\r\n]+|(?<=[；;。])|(?=\s*\d+[.、])|(?=任职要求：)",
        clean_jd,
    )
    candidates = []
    for raw in raw_lines:
        line = re.sub(r"^\s*(?:[-•·]|[（(]?\d+[）).、])\s*", "", raw).strip(" \t；;。")
        line = re.sub(r"^(?:岗位职责|主要职责|任职要求)\s*[：:]?\s*", "", line).strip()
        if len(line) < 6 or len(line) > 180:
            continue
        if _non_scoring_requirement_reason(line) or any(label in line for label in (
            "工作地点", "薪资福利", "投递邮箱", "联系方式", "请备注",
            "团队氛围", "亲自带教", "有问必答", "留用机会",
        )):
            continue
        if ("招聘" in line or "岗位描述" in line) and not any(
            verb in line for verb in ("负责", "协助", "参与", "要求", "熟练", "具备")
        ):
            continue
        if line in candidates:
            continue
        candidates.append(line)
    if not candidates:
        candidates = [re.sub(r"\s+", " ", jd or "").strip()[:180]]

    background_lower = (background or "").lower()
    skill_terms = (
        "python", "sql", "excel", "wind", "vba", "英语", "英文", "数据", "建模",
        "财务", "估值", "研究", "产品", "项目", "沟通", "协调", "ai", "投行",
        "ipo", "债券", "发行", "承销", "并购", "尽调",
    )
    gaps = []
    for requirement in candidates[:8]:
        matched_terms = [term for term in skill_terms
                         if term in requirement.lower() and term in background_lower]
        status = "partial" if matched_terms else "missing"
        dimension = "硬技能" if any(x in requirement.lower() for x in
                                   ("python", "sql", "excel", "wind", "vba", "数据", "建模")) else "岗位要求"
        gaps.append({
            "dimension": dimension,
            "requirement": requirement,
            "my_status": status,
            "severity": "fixable",
            "plan_type": "pitch" if matched_terms else "knowledge",
            "note": ("候选人背景中检索到相关关键词：" + "、".join(matched_terms)
                     if matched_terms else "尚未自动找到直接证据，需人工确认或补充"),
        })
    return {"gaps": gaps, "fallback_used": True}


@app.post("/api/chat/actions/propose")
def propose_chat_actions(body: ChatActionProposalIn):
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位不存在")
    history = db.get_chat_history(body.session_id, limit=24)
    if not history:
        return {"summary": "当前还没有可整理的对话", "actions": []}
    # Turning an existing answer into a write proposal must be local and
    # deterministic. A second model call made this button slower and less
    # reliable than the conversation that produced the answer.
    data = _fallback_chat_actions(history, body.track_id)
    allowed = {"create_knowledge", "create_gap", "save_feedback"}
    actions = []
    for item in data.get("actions") or []:
        if item.get("action_type") not in allowed:
            continue
        if item.get("action_type") == "create_gap" and not body.track_id:
            continue
        item["track_id"] = body.track_id
        actions.append(item)
    return {"summary": data.get("summary") or "", "actions": actions[:5]}


@app.post("/api/chat/actions/apply")
def apply_chat_actions(body: ChatActionApplyIn):
    track = db.get_job_track(body.track_id) if body.track_id else None
    if body.track_id and not track:
        raise HTTPException(404, "岗位不存在")
    history = db.get_chat_history(body.session_id, limit=40)
    latest_answer = next((m.get("content") for m in reversed(history)
                          if m.get("role") == "assistant"), "")
    markdown_blocks = [x.strip() for x in re.findall(
        r"```(?:markdown|md)?[ \t]*\n([\s\S]*?)```", latest_answer,
        flags=re.IGNORECASE,
    ) if x.strip()]
    knowledge_actions = [x for x in body.actions[:10] if x.get("action_type") == "create_knowledge"]
    knowledge_cursor = 0
    results = []
    documents = []
    for item in body.actions[:10]:
        action_type = item.get("action_type")
        if action_type == "create_knowledge":
            title = (item.get("title") or "对话沉淀").strip()[:100]
            if not latest_answer:
                continue
            block_index = item.get("source_block_index")
            try:
                block_index = int(block_index) if block_index is not None else None
            except (TypeError, ValueError):
                block_index = None
            if block_index is not None and 0 <= block_index < len(markdown_blocks):
                knowledge_body = markdown_blocks[block_index]
            elif len(knowledge_actions) > 1 and knowledge_cursor < len(markdown_blocks):
                knowledge_body = markdown_blocks[knowledge_cursor]
            else:
                knowledge_body = latest_answer
            knowledge_cursor += 1
            folder_id = None
            if body.track_id:
                folders, _ = db.ensure_track_knowledge_folders(body.track_id)
                folder = next((x for x in folders if x.get("name") == "面试准备"), None)
                folder_id = folder.get("id") if folder else None
            knowledge_id = db.create_knowledge_item({
                "title": title, "content": knowledge_body,
                "scope_type": "track" if body.track_id else "global",
                "track_id": body.track_id, "folder_id": folder_id,
                "topic": item.get("topic") or "对话沉淀",
                "mastery": "learning", "status": "active",
            })
            results.append(f"新增知识：{title}")
            if track:
                workspace_name = " · ".join(
                    x for x in ((track.get("company") or "").strip(),
                                (track.get("role") or track.get("target") or "").strip()) if x
                ) or "当前岗位"
                document_path = f"知识空间 / {workspace_name} / 面试准备 / {title}"
            else:
                document_path = f"知识空间 / 个人通用 / {title}"
            documents.append({
                "id": knowledge_id,
                "title": title,
                "operation": "created",
                "operation_label": "新建文档",
                "path": document_path,
                "scope_type": "track" if body.track_id else "global",
                "track_id": body.track_id,
            })
        elif action_type == "create_gap" and body.track_id:
            requirement = (item.get("requirement") or "").strip()
            if not requirement:
                continue
            db.create_track_gap({
                "track_id": body.track_id,
                "dimension": item.get("dimension") or "准备事项",
                "requirement": requirement,
                "my_status": "missing",
                "severity": item.get("severity") if item.get("severity") in {"blocker", "fixable", "nice"} else "fixable",
                "status": "todo", "note": item.get("reason"),
            })
            results.append(f"新增岗位差距：{requirement[:60]}")
        elif action_type == "save_feedback":
            directive = (item.get("directive") or "").strip()
            if not directive:
                continue
            category = item.get("category") or "emphasis"
            strength = item.get("strength") if item.get("strength") in {"hard", "soft"} else "soft"
            saved = _save_feedback_with_harness({
                "scope": "track" if body.track_id else "global",
                "scope_id": body.track_id,
                "original_text": directive,
                "directive": directive,
                "category": category,
                "polarity": "avoid" if category == "avoidance" else "do",
                "strength": strength,
                "apply_stale": True,
            })
            stale = saved.get("impact", {}).get("stale_changed") or 0
            suffix = f"，{stale} 个旧产物待更新" if stale else ""
            results.append(f"新增反馈约束：{directive[:60]}{suffix}")
    if results:
        _commit("聊天 Agent 执行：" + "；".join(results)[:100])
    return {"ok": True, "results": results, "documents": documents}


# ─── 对话 → 真落库（核心：AI 提议结构化改动，用户确认后写入）────────────────

PROPOSE_PROMPT = """你是 Caddie 的「入库决策器」。请先合并事实、检查完整性，再决定能否写库。

严格规则：
1. 用户原话是唯一事实源；Caddie 的历史回答只是草稿，不能覆盖用户原话。只有用户明确说
   “把这个更新进去 / 按这个版本写入 / merge_to_existing”等确认语句时，紧邻的候选稿才可作为本次更新正文。
2. 只处理“当前任务片段”，不得把更早的其他岗位或项目混进来。
3. 越晚出现的用户修正优先级越高。例如“这个是东方证券”必须覆盖此前的“未具名券商”。
4. 不得把“头部券商、某公司、未具名机构”等描述当作正式公司名。仍无法确认时必须追问，ready_to_apply=false。
5. 用户粘贴了完整 JD 时，job_description 必须保留完整原文，不得只存摘要；链接、来源、状态也要保留。
6. 写入前检查当前数据快照。相同岗位已存在时优先 update_application，不得重复 create_application。
   用户明确要求把两条投递合并时，必须返回 merge_application，而不是只更新其中一条。
7. 项目归并优先于新建：资料标题不同不代表项目不同。必须比较目标问题、工作对象、时间线、产出物和已有项目；
   若是已有项目的详细版、迭代版、面试版或子模块，一律 update_project，不得 create_project。
8. 收件箱资料已有完整 Markdown 时，不要把正文压缩后塞进 JSON。项目改动只返回 primary_source_id，
   后端会直接读取该资料的完整原文；其他同项目资料放 supporting_source_ids，作为证据关联而不是新项目。
9. 使用本地原始文件时，只能返回文件标题旁标注的整数 primary_local_candidate_id；绝对不能把文件路径
   填入 primary_source_id、project_id 或 experience_id。后端会按候选 ID 读取完整原文。
10. 只有目标问题和交付物均独立、可单独讲述的一条工作线才可新建项目。无法判断时必须追问，不得猜测拆分。
11. primary_source_id 必须来自“最近收件箱资料”中明确列出的资料 id；primary_local_candidate_id
   必须来自“本任务实际读取的本地原始文件”中明确列出的候选 ID。二者不得混用。
   knowledge_item 的 id 是知识文档编号，绝不能填入 primary_source_id；参考知识文档时使用
   reference_knowledge_id，并为每个目标项目分别返回独立的完整 document。
   当用户确认的是对话中的候选稿且没有对应原始资料时，update_project 必须直接返回完整新版 document，
   不得为了满足字段格式臆造资料 ID。

可用的改动类型（type）：
- create_experience: 字段 company, role, start_date, end_date；可选 projects:[{name,one_liner,document,technologies,keywords}]
- create_project: 字段 experience_id（必须引用下面快照里的真实 id）, name, one_liner, technologies, keywords；
  若来自收件箱，必须给 primary_source_id；若来自本地文件，必须给 primary_local_candidate_id；
  可选 supporting_source_ids，不要返回 document
- update_project: 字段 project_id（真实 id）, 以及要更新的字段；若来自收件箱，必须给 primary_source_id，
  若来自本地文件，必须给 primary_local_candidate_id；可选 supporting_source_ids，不要返回 document；
  若参考已有知识文档，使用 reference_knowledge_id，并返回该项目独立的完整新版 document；
  非资料型修改也返回完整新版 document
- create_application: 字段 company, role, industry, status(applied/screening/written/interview/offer/rejected), source, applied_date, job_description, apply_url, notes, location
- update_application: 字段 application_id（真实 id）以及需要纠正/补充的上述字段
- merge_application: 字段 source_application_id（删除的重复记录）、target_application_id（保留的完整记录），
  以及合并后的 company、role、status、source、notes、remark_tag 等字段
- set_application_status: 字段 application_id（真实 id）, status
- add_interview: 字段 application_id（真实 id）, round_type, feedback, outcome

%s

当前任务片段：
%s

只返回如下 JSON，不要任何多余文字：
{
  "summary": "用公司、岗位和项目名称说明这次要改什么，不得出现 id、字段名或内部指令；如果没有任何需要入库的信息就写：无",
  "ready_to_apply": true,
  "clarifications": [
    {
      "id": "稳定的英文标识",
      "question": "只问一个明确问题",
      "type": "single 或 multiple",
      "options": [
        {"label": "用户看到的短选项", "value": "写回对话的完整答案", "recommended": true}
      ],
      "allow_custom": true
    }
  ],
  "warnings": [],
  "changes": [ { "type": "...", ... } ]
}
缺少关键事实或存在冲突时：ready_to_apply=false，clarifications 必须输出可直接作答的问题卡，changes 可保留草案但不得猜测。
问题卡规则：
- 能从当前对话或数据快照归纳候选答案时，必须给出 2-5 个互斥选项，不要让用户重新打一遍已经说过的话。
- “哪些项目属于这段经历”一类问题使用 multiple，并把识别到的项目逐项列为选项。
- “是否合并”“采用哪个名称”一类问题使用 single。
- 证据更充分的选项标 recommended=true，但不得替用户自动选择。
- 只有姓名、日期等无法合理预设的事实才允许 options 为空，此时 allow_custom=true。
- 每张卡只确认一个决策，问题和选项都要短、具体、可操作。
若没有需要入库的内容，changes 返回空数组 []。"""


_PROPOSAL_ANCHOR_TERMS = (
    "岗位职责", "职位描述", "招聘对象", "任职要求", "工作内容",
    "实习岗位", "项目背景", "项目经历", "面试反馈", "工作总结",
)
_PROPOSAL_WRITE_TERMS = (
    "新建一条求职线", "新建求职线", "写入档案", "写入职业档案", "整理入库",
    "合并", "更新进度", "更新一下进度", "更正状态", "状态是错的", "误判",
)
_PLACEHOLDER_COMPANY_TERMS = (
    "未具名", "待确认", "某公司", "某大型", "某头部", "头部券商",
    "top券商", "top 券商", "某券商", "某机构",
)


def _proposal_task_window(history):
    """Keep the latest substantive task and its corrections, not the whole session."""
    if not history:
        return []
    user_indexes = [i for i, item in enumerate(history) if item.get("role") == "user"]
    anchor = max(0, len(history) - 24)
    # A clarification answer can be long, but it is not a new task. Start at
    # the latest explicit archive/write request so all later corrections stay
    # in one decision context.
    selected_anchor = None
    for i in reversed(user_indexes):
        text = (history[i].get("content") or "").strip()
        if text.startswith("针对写入前的确认"):
            continue
        if any(term in text for term in _PROPOSAL_WRITE_TERMS):
            selected_anchor = i
            anchor = i
            break
    if selected_anchor is not None:
        # Follow-up commands such as “merge it” belong to the same write task.
        # Include a nearby initiating request so company/location facts are not
        # discarded when the user refines the operation.
        nearby = [i for i in user_indexes if max(0, selected_anchor - 8) <= i < selected_anchor]
        for i in nearby:
            text = (history[i].get("content") or "").strip()
            if any(term in text for term in _PROPOSAL_WRITE_TERMS):
                anchor = i
                break
    else:
        for i in reversed(user_indexes):
            text = (history[i].get("content") or "").strip()
            if text.startswith("针对写入前的确认"):
                continue
            if len(text) >= 180 or any(term in text for term in _PROPOSAL_ANCHOR_TERMS):
                anchor = i
                break
    return history[anchor:]


def _confirmed_profile_facts(history):
    """Extract facts the user already confirmed; later answers always win."""
    facts = {}
    pairs = []
    for item in history:
        if item.get("role") != "user":
            continue
        text = item.get("content") or ""
        pairs.extend(re.findall(r"(?:^|\n)\s*\d+\.\s*(.+?)\s*\n答：\s*(.+?)(?=\n\s*\d+\.|\Z)", text, re.S))
    for question, answer in pairs:
        answer = re.sub(r"\s+", " ", answer).strip(" ；;。")
        if not answer:
            continue
        if "公司" in question or "机构" in question:
            facts["company"] = answer
        if "岗位名称" in question or "具体岗位" in question or "哪个岗位" in question:
            facts["role"] = answer
        if "地点" in question or "base" in question.lower():
            facts["location"] = answer
        if "投递来源" in question or "渠道" in question:
            facts["source"] = answer
        if any(term in question for term in ("当前进展", "当前状态", "仍在面试", "面试通过", "offer")):
            facts["progress"] = answer

    user_text = "\n".join(
        item.get("content") or "" for item in history if item.get("role") == "user"
    )
    direct_roles = re.findall(
        r"(?:就是|岗位(?:名称)?(?:是|为)|更新岗位名称为)\s*[：:]?\s*([^，。；\n]{2,30}?(?:实习生|实习岗|岗位))",
        user_text,
    )
    if direct_roles and not facts.get("role"):
        facts["role"] = direct_roles[-1].strip()
    facts["status_correction"] = any(term in user_text for term in (
        "状态是错的", "状态错了", "rejected状态是错的", "已拒绝为误判",
        "已拒绝\”为误判", "之前的rejected", "请更新为最新进展", "更正为当前",
    ))
    return facts


def _clarification_is_resolved(question, facts):
    question = question or ""
    return bool(
        (facts.get("company") and any(x in question for x in ("公司", "机构")))
        or (facts.get("role") and any(x in question for x in ("岗位名称", "具体岗位", "哪个岗位")))
        or (facts.get("location") and any(x in question for x in ("地点", "base")))
        or (facts.get("source") and any(x in question for x in ("投递来源", "渠道")))
        or (facts.get("progress") and any(x in question for x in ("当前进展", "当前状态", "面试通过", "offer")))
    )


def _proposal_inbox_catalog(history):
    user_text = "\n".join(
        item.get("content") or "" for item in history if item.get("role") == "user"
    )
    if not any(term in user_text for term in ("收件箱", "刚上传", "刚导入", "新上传", "新导入")):
        return ""
    parts = ["\n【最近收件箱资料：用于判断项目归属】"]
    for brief in db.list_sources(limit=12):
        source = db.get_source(brief.get("id")) or {}
        content = (source.get("content") or "").strip()
        parts.append(
            f"\n资料 id={source.get('id')}：{source.get('title') or source.get('file_name') or '未命名'}"
            f"（正文 {len(content)} 字）\n"
            f"摘要：{source.get('summary') or '无'}\n"
            f"开头摘录：{content[:2200]}"
        )
    parts.append(
        "\n注意：摘录只用于归属判断。写入时必须返回 primary_source_id，"
        "后端会读取未截断全文，禁止依据摘录重写或概括正文。"
    )
    return "\n".join(parts)


def _proposal_local_evidence(session_id, task_history):
    message_ids = [item.get("id") for item in task_history if item.get("id")]
    linked = db.get_message_local_files(message_ids)
    candidate_ids = list(dict.fromkeys(
        int(item["id"]) for item in linked if item.get("id")
    ))
    if not candidate_ids:
        session = db.get_session(session_id) or {}
        run_id = session.get("workspace_run_id")
        if run_id:
            query = "\n".join(
                item.get("content") or "" for item in task_history if item.get("role") == "user"
            )
            candidate_ids = _select_workspace_candidates(run_id, query, limit=10)
    if not candidate_ids:
        return "", []
    text, refs, issues = _load_chat_local_context(
        candidate_ids, total_limit=60000, per_file_limit=12000
    )
    if not text:
        return "", refs
    issue_text = "\n".join(f"- {item}" for item in issues) if issues else "无"
    return (
        "\n\n【本任务实际读取的本地原始文件——高于 Caddie 草稿，必须作为写入证据】\n"
        f"{text}\n\n读取异常：\n{issue_text}\n"
        "规则：不得再声称用户未提供原始资料；不得以 Caddie 草稿为事实依据；"
        "写入建议必须优先保留本地原文细节并按同一业务目标归并项目。"
    ), refs


def _latest_company_correction(history):
    patterns = (
        r"(?:这个|公司|机构)(?:是|叫|为)\s*[：:]?\s*([^，。；\n]{2,30}?)(?:哈|呀|哦|的|$)",
        r"(?:公司名称|实习机构)\s*[：:]\s*([^，。；\n]{2,50})",
    )
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        text = item.get("content") or ""
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip(" ：:，。")
    return ""


def _company_is_placeholder(value):
    value = (value or "").strip().lower()
    return not value or any(term.lower() in value for term in _PLACEHOLDER_COMPANY_TERMS)


def _normalize_role_for_match(value):
    return re.sub(r"[\s（）()·—_/\\-]|实习生|实习", "", value or "").lower()


def _normalize_clarifications(items):
    normalized = []
    seen = set()
    for index, raw in enumerate(items or []):
        if isinstance(raw, str):
            question = raw.strip()
            item = {
                "id": f"question_{index + 1}",
                "question": question,
                "type": "single",
                "options": [],
                "allow_custom": True,
            }
        elif isinstance(raw, dict):
            question = str(raw.get("question") or raw.get("text") or "").strip()
            item = {
                "id": re.sub(r"[^a-zA-Z0-9_-]+", "_", str(raw.get("id") or f"question_{index + 1}"))[:50],
                "question": question,
                "type": "multiple" if raw.get("type") == "multiple" else "single",
                "options": [],
                "allow_custom": bool(raw.get("allow_custom", True)),
            }
            for option in raw.get("options") or []:
                if isinstance(option, str):
                    label = value = option.strip()
                    recommended = False
                elif isinstance(option, dict):
                    label = str(option.get("label") or option.get("value") or "").strip()
                    value = str(option.get("value") or label).strip()
                    recommended = bool(option.get("recommended"))
                else:
                    continue
                if label and not any(x["value"] == value for x in item["options"]):
                    item["options"].append({
                        "label": label[:80],
                        "value": value[:500],
                        "recommended": recommended,
                    })
        else:
            continue
        key = re.sub(r"\s+", "", item["question"]).lower()
        if not item["question"] or key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _resolve_local_candidate(value):
    """Resolve a candidate id or a legacy model-emitted local path."""
    candidate_id = _positive_int(value)
    if candidate_id:
        return db.get_file_candidate(candidate_id)
    raw = str(value or "").strip()
    if not raw:
        return None
    target = str(Path(raw).expanduser())
    candidates = db.list_file_candidates(limit=2000)
    exact = [
        item for item in candidates
        if str(Path(item.get("file_path") or "").expanduser()) == target
    ]
    if exact:
        return exact[0]
    target_path = Path(target)
    matches = []
    for item in candidates:
        path = Path(item.get("file_path") or "").expanduser()
        path_text = str(path)
        if (
            path_text.startswith(target)
            or target.startswith(str(path.with_suffix("")))
            or path.stem == target_path.name
        ):
            matches.append(item)
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            Path(item.get("file_path") or "").exists(),
            Path(item.get("file_path") or "").stat().st_size
            if Path(item.get("file_path") or "").exists() else 0,
        ),
        reverse=True,
    )
    return matches[0]


def _materialize_local_project_changes(changes):
    """Turn local-file references into full project documents before DB writes."""
    materialized = []
    notes = []
    for raw in changes or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if item.get("type") not in {"create_project", "update_project"}:
            materialized.append(item)
            continue

        local_ref = item.get("primary_local_candidate_id")
        source_ref = item.get("primary_source_id")
        if local_ref is None and source_ref is not None and _positive_int(source_ref) is None:
            local_ref = source_ref

        candidate = _resolve_local_candidate(local_ref) if local_ref is not None else None
        if candidate:
            path = Path(candidate.get("file_path") or "").expanduser()
            if not path.exists() or not path.is_file():
                raise ValueError(f"本地原始文件已不存在：{path}")
            text = _extract_text(path.read_bytes(), path.name).strip()
            if not text:
                raise ValueError(f"本地原始文件没有可写入文字：{path.name}")
            item["document"] = text
            item["primary_local_candidate_id"] = candidate.get("id")
            item.pop("primary_source_id", None)
            notes.append(f"已读取本地原文：{path.name}（{len(text)} 字）")
        elif source_ref is not None and _positive_int(source_ref) is None:
            raise ValueError(f"无法定位本地原始文件：{source_ref}")

        # Never let model-emitted paths reach SQLite integer fields.
        numeric_source = _positive_int(item.get("primary_source_id"))
        if numeric_source:
            item["primary_source_id"] = numeric_source
        else:
            item.pop("primary_source_id", None)
        supporting = item.get("supporting_source_ids") or item.get("source_ids") or []
        if not isinstance(supporting, (list, tuple, set)):
            supporting = [supporting]
        item["supporting_source_ids"] = [
            value for value in (_positive_int(x) for x in supporting) if value
        ]
        materialized.append(item)
    return materialized, notes


def _recover_confirmed_project_documents(changes, session_id):
    """Repair stale source references from a user-confirmed project draft.

    A proposal can occasionally confuse a local candidate id with a source id.
    If the user has explicitly confirmed merging the current draft, recover the
    longest matching Markdown section from that same session instead of asking
    them to repeat the entire workflow.
    """
    if not session_id:
        return changes, []
    history = db.get_chat_history(session_id, limit=40)
    user_text = "\n".join(
        item.get("content") or "" for item in history if item.get("role") == "user"
    )
    confirmed = any(term in user_text for term in (
        "merge_to_existing", "把这个更新进去", "按这个版本写入",
        "确认写入", "写入职业档案",
    ))
    if not confirmed:
        return changes, []

    assistant_docs = [
        item.get("content") or "" for item in history if item.get("role") == "assistant"
    ]
    repaired, notes = [], []

    def normalized(value):
        return re.sub(r"[\s·—_《》“”\"'（）()：:，,]+", "", value or "").lower()

    def candidate_sections(text):
        matches = list(re.finditer(r"(?m)^###\s+(.+?)\s*$", text or ""))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            yield match.group(1).strip(), text[match.start():end].strip().rstrip("-").strip()

    for raw in changes or []:
        item = dict(raw)
        if item.get("type") != "update_project":
            repaired.append(item)
            continue
        source_id = _positive_int(item.get("primary_source_id"))
        source = db.get_source(source_id) if source_id else None
        if not source_id or (source and (source.get("content") or "").strip()):
            repaired.append(item)
            continue

        project = db.get_project(_positive_int(item.get("project_id"))) or {}
        project_name = item.get("name") or project.get("name") or ""
        project_key = normalized(project_name)
        anchors = {
            project_key,
            normalized(project_name.split("·", 1)[0]),
            normalized(project_name.split("—", 1)[0]),
        }
        anchors = {x for x in anchors if len(x) >= 6}
        matched = []
        for message_index, text in enumerate(assistant_docs):
            for heading, section in candidate_sections(text):
                heading_key = normalized(heading)
                if any(anchor in heading_key or heading_key in anchor for anchor in anchors):
                    matched.append({
                        "section": section,
                        "message_index": message_index,
                        "is_followup": "**追问可参考：**" in section,
                    })
        if not matched:
            repaired.append(item)
            continue

        # Keep one final detailed version, not every intermediate rewrite.
        # A later follow-up-only section is appended because it is a distinct
        # deliverable rather than another competing project draft.
        detail_sections = [x for x in matched if not x["is_followup"]]
        followup_sections = [x for x in matched if x["is_followup"]]
        unique_sections = []
        if detail_sections:
            unique_sections.append(max(
                detail_sections, key=lambda x: len(x["section"])
            )["section"])
        if followup_sections:
            unique_sections.append(max(
                followup_sections,
                key=lambda x: (x["message_index"], len(x["section"])),
            )["section"])
        item.pop("primary_source_id", None)
        item.pop("primary_local_candidate_id", None)
        item["document"] = "\n\n---\n\n".join(unique_sections)
        repaired.append(item)
        notes.append(
            f"已从本轮确认稿恢复项目正文：{project_name}（{len(item['document'])} 字）"
        )
    return repaired, notes


_PROJECT_FOLLOWUP_HEADING = re.compile(
    r"(?im)^\s*(?:#{1,6}\s+|\*\*)?"
    r"(?:面试准备\s*[：:]?\s*)?"
    r"(?:面试高频追问与应对|高频追问与应对|高频追问(?:\s*&\s*我的答法)?|"
    r"可能被追问(?:的问题)?|面试追问|追问题库)"
    r"(?:\*\*)?\s*$"
)


def _split_numbered_project_followups(document: str) -> tuple[str, list[dict]]:
    """Separate interview-question sections from a project fact document.

    Supports both Q1/A1 blocks and ordinary numbered question lists.  The
    latter is what the project workspace renders in existing Caddie drafts.
    """
    text = str(document or "")
    headings = list(_PROJECT_FOLLOWUP_HEADING.finditer(text))
    if not headings:
        return text, []
    extracted = []
    spans = []
    for heading in headings:
        following_heading = re.search(r"(?m)^\s*#{1,6}\s+\S.*$", text[heading.end():])
        end = heading.end() + following_heading.start() if following_heading else len(text)
        section = text[heading.end():end].strip()
        entries = list(re.finditer(
            r"(?m)^\s*(?:\*\*)?"
            r"(?:(?:Q\s*)?(\d+)\s*[.、：:]|[-*]\s+)\s*"
            r"(.+?[?？])(?:\*\*)?[ \t]*$",
            section,
        ))
        for index, match in enumerate(entries):
            entry_end = entries[index + 1].start() if index + 1 < len(entries) else len(section)
            trailing = section[match.end():entry_end].strip()
            answer_match = re.search(
                r"(?is)^(?:(?:A\s*\d*|答(?:法)?|参考回答|建议答法)\s*[：:]\s*|>\s*)"
                r"(.+)$",
                trailing,
            )
            answer = answer_match.group(1).strip() if answer_match else ""
            question = re.sub(r"\s+", " ", match.group(2)).strip()
            if len(question) >= 4:
                extracted.append({
                    "question": question[:300],
                    "answer": answer[:2000],
                    "status": "todo",
                    "category": "项目判断",
                    "origin": "project_document_split",
                    "kind": "reflective",
                })
        spans.append((heading.start(), end))
    cleaned = text
    for start, end in reversed(spans):
        cleaned = cleaned[:start].rstrip() + "\n\n" + cleaned[end:].lstrip()
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip(), extracted


def _remove_selected_project_followups(document: str, selected_questions: set[str]) -> str:
    """Remove selected Q&A blocks while preserving unselected source text."""
    text = str(document or "")
    replacements = []
    headings = list(_PROJECT_FOLLOWUP_HEADING.finditer(text))
    for heading in headings:
        following_heading = re.search(r"(?m)^\s*#{1,6}\s+\S.*$", text[heading.end():])
        end = heading.end() + following_heading.start() if following_heading else len(text)
        section = text[heading.end():end]
        entries = list(re.finditer(
            r"(?m)^\s*(?:\*\*)?"
            r"(?:(?:Q\s*)?(\d+)\s*[.、：:]|[-*]\s+)\s*"
            r"(.+?[?？])(?:\*\*)?[ \t]*$",
            section,
        ))
        remove_spans = []
        for index, match in enumerate(entries):
            block_end = entries[index + 1].start() if index + 1 < len(entries) else len(section)
            key = re.sub(r"\s+", "", match.group(2)).lower()
            if key in selected_questions:
                remove_spans.append((match.start(), block_end))
        if not remove_spans:
            continue
        remaining = section
        for start, stop in reversed(remove_spans):
            remaining = remaining[:start].rstrip() + "\n\n" + remaining[stop:].lstrip()
        if not remaining.strip():
            replacements.append((heading.start(), end, ""))
        else:
            replacements.append((heading.end(), end, "\n\n" + remaining.strip() + "\n\n"))
    for start, stop, value in reversed(replacements):
        text = text[:start] + value + text[stop:]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _prepare_proposed_changes(data, task_history):
    changes = data.get("changes") if isinstance(data.get("changes"), list) else []
    user_texts = [(item.get("content") or "") for item in task_history if item.get("role") == "user"]
    source_text = "\n\n".join(user_texts).strip()
    anchor_text = user_texts[0].strip() if user_texts else ""
    company_correction = _latest_company_correction(task_history)
    confirmed = _confirmed_profile_facts(task_history)
    urls = re.findall(r"https?://[^\s，。；）)]+", source_text)
    existing_apps = db.get_applications()
    clarifications = list(data.get("clarifications") or [])
    warnings = list(data.get("warnings") or [])
    prepared = []
    separated_followups = []
    followup_decisions = []

    for raw in changes:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if item.get("type") in {"create_project", "update_project"}:
            primary_source_id = item.get("primary_source_id")
            primary_source_id = _positive_int(primary_source_id)
            if primary_source_id:
                source = db.get_source(primary_source_id)
                if source and (source.get("content") or "").strip():
                    item["primary_source_id"] = primary_source_id
                else:
                    item.pop("primary_source_id", None)
                    knowledge = db.get_knowledge_item(primary_source_id)
                    if knowledge:
                        item["reference_knowledge_id"] = primary_source_id
                        warnings.append(
                            f"#{primary_source_id} 是知识文档「{knowledge.get('title') or ''}」，"
                            "已移除错误的原始资料引用。"
                        )
                    else:
                        warnings.append(
                            f"已移除无效的主资料引用 #{primary_source_id}；"
                            "资料 ID 与本地文件候选 ID 不能混用。"
                        )
                    if not (item.get("document") or "").strip():
                        clarifications.append({
                            "id": f"invalid_project_source_{item.get('project_id') or len(prepared) + 1}",
                            "question": (
                                f"知识文档「{knowledge.get('title')}」包含多个项目，是否为当前项目单独生成完整正文？"
                                if knowledge else
                                "这项更新引用了失效资料，是否改用本轮已经确认的候选稿重新整理？"
                            ),
                            "type": "single",
                            "options": [{
                                "label": "分别生成并合并",
                                "value": (
                                    "请读取参考知识文档，为每个现有项目分别生成独立完整正文，不新增项目"
                                    if knowledge else
                                    "请忽略失效的资料 ID，基于本轮已确认的候选稿返回完整新版 document"
                                ),
                                "recommended": True,
                            }],
                            "allow_custom": False,
                        })
                    primary_source_id = None
            elif item.get("primary_source_id") is not None:
                candidate = _resolve_local_candidate(item.get("primary_source_id"))
                if candidate:
                    item["primary_local_candidate_id"] = candidate.get("id")
                    warnings.append(
                        f"已把本地路径纠正为文件候选 #{candidate.get('id')}，写入时将读取完整原文。"
                    )
                item.pop("primary_source_id", None)
            if primary_source_id:
                linked_project = next((
                    link for link in db.list_source_links(source_id=primary_source_id)
                    if link.get("entity_type") == "project" and db.get_project(link.get("entity_id"))
                ), None)
                if linked_project:
                    project = db.get_project(linked_project["entity_id"]) or {}
                    if item.get("type") == "create_project" or item.get("project_id") != project.get("id"):
                        warnings.append(
                            f"资料 #{primary_source_id} 已属于项目 #{project.get('id')}「{project.get('name')}」，"
                            "已强制改为更新，避免重复建项目。"
                        )
                    item["type"] = "update_project"
                    item["project_id"] = project.get("id")
                    item["name"] = project.get("name")
            if item.get("type") == "update_project":
                current_project = db.get_project(_positive_int(item.get("project_id"))) or {}
                if current_project:
                    item["name"] = current_project.get("name") or item.get("name")
                    experience = db.get_experience(current_project.get("experience_id")) or {}
                    item["display_title"] = item.get("name") or "现有项目"
                    item["display_context"] = " · ".join(filter(None, [
                        experience.get("company"), experience.get("role"),
                    ]))
                    item["display_detail"] = (
                        "更新项目详细内容并保留现有项目结构"
                        if (item.get("document") or item.get("reference_knowledge_id")
                            or item.get("primary_source_id") or item.get("primary_local_candidate_id"))
                        else "补充项目信息"
                    )
                    if (item.get("document") or "").strip():
                        original_document = item["document"]
                        clean_document, followups = _split_numbered_project_followups(original_document)
                        if followups:
                            existing_questions = {
                                re.sub(r"\s+", "", str(row.get("question") or "")).lower()
                                for row in db.list_followups(project_id=current_project["id"])
                            }
                            new_followups = []
                            for followup in followups:
                                key = re.sub(r"\s+", "", followup["question"]).lower()
                                if key in existing_questions:
                                    continue
                                existing_questions.add(key)
                                new_followups.append(followup)
                            if new_followups:
                                followup_decisions.append({
                                    "type": "followup_migration",
                                    "count": len(new_followups),
                                    "project_id": current_project["id"],
                                    "project_name": current_project.get("name"),
                                    "target_path": (
                                        f"我的经历 / {current_project.get('name') or '当前项目'} / 面试准备"
                                    ),
                                    "items": new_followups,
                                    "project_change": item,
                                    "clean_document": clean_document,
                                })
                                warnings.append(
                                    f"检测到 {len(new_followups)} 组面试问答，请选择保留或迁移方式。"
                                )
        if item.get("type") in {"create_application", "update_application"}:
            if company_correction or confirmed.get("company"):
                item["company"] = company_correction or confirmed.get("company")
            if confirmed.get("role"):
                item["role"] = confirmed["role"]
            if confirmed.get("location"):
                item["location"] = confirmed["location"]
            if confirmed.get("source"):
                item["source"] = confirmed["source"]
            progress = confirmed.get("progress", "")
            if "面试流程" in progress or "筛选" in progress or "screening" in progress.lower():
                item["status"] = "screening"
            if anchor_text and any(term in anchor_text for term in ("职位描述", "岗位职责", "工作内容")):
                item["job_description"] = anchor_text
            if urls and not item.get("apply_url"):
                item["apply_url"] = urls[-1]
            if "实习僧" in source_text:
                item["source"] = "实习僧"
            if "已投递" in source_text:
                item["status"] = "applied"

            role_key = _normalize_role_for_match(item.get("role"))
            if item.get("type") == "create_application" and role_key:
                explicit_merge = any(term in source_text for term in (
                    "同一个岗位", "二者合并", "两个合并", "把二者合并", "之前的记录",
                ))
                match = next((
                    app for app in existing_apps
                    if (
                        _normalize_role_for_match(app.get("role")) == role_key
                        or (
                            min(len(_normalize_role_for_match(app.get("role"))), len(role_key)) >= 5
                            and (
                                _normalize_role_for_match(app.get("role")) in role_key
                                or role_key in _normalize_role_for_match(app.get("role"))
                            )
                        )
                        or (
                            explicit_merge
                            and "股权" in role_key
                            and "股权" in _normalize_role_for_match(app.get("role"))
                        )
                    )
                    and (_company_is_placeholder(app.get("company"))
                         or (item.get("company") or "") == (app.get("company") or ""))
                ), None)
                if match:
                    item["type"] = "update_application"
                    item["application_id"] = match["id"]
                    warnings.append(f"检测到同岗位投递 #{match['id']}，将补充或纠正现有记录，避免重复创建。")

            if item.get("type") == "update_application":
                current = db.get_application(_positive_int(item.get("application_id"))) or {}
                changed_status = item.get("status") and item.get("status") != current.get("status")
                # Never trust a model-emitted bypass. The server signs this
                # flag only when the user explicitly corrected prior history.
                item.pop("allow_status_correction", None)
                if changed_status and confirmed.get("status_correction"):
                    item["allow_status_correction"] = True
                    warnings.append(
                        f"将把投递 #{current.get('id')} 的历史状态“{current.get('status')}”按用户纠正为“{item.get('status')}”。"
                    )

            if _company_is_placeholder(item.get("company")):
                clarifications.append("这份岗位的正式公司名称是什么？目前只有“头部券商/未具名机构”等占位描述。")
            if not (item.get("role") or "").strip():
                clarifications.append("需要确认具体岗位名称后才能写入。")
            if anchor_text and any(term in anchor_text for term in ("职位描述", "岗位职责")) \
                    and len((item.get("job_description") or "").strip()) < 100:
                clarifications.append("检测到你粘贴了完整 JD，但写入草案没有保留完整原文，请重新整理。")
        prepared.append(item)

    explicit_merge = any(term in source_text for term in (
        "二者合并", "两个合并", "把二者合并", "合并这两条", "合并到原记录",
        "是同一个岗位", "同一个岗位",
    ))
    if explicit_merge:
        company = confirmed.get("company") or company_correction
        role = confirmed.get("role") or ""
        same_company = [
            app for app in existing_apps
            if company and (app.get("company") or "").strip() == company.strip()
        ]
        role_key = _normalize_role_for_match(role)
        target = next((
            app for app in same_company
            if role_key and (
                _normalize_role_for_match(app.get("role")) == role_key
                or ("股权" in role_key and "股权" in _normalize_role_for_match(app.get("role")))
            )
        ), None)
        source = next((
            app for app in sorted(same_company, key=lambda value: value.get("id") or 0, reverse=True)
            if target and app.get("id") != target.get("id")
            and any(term in (app.get("role") or "") for term in ("投行部", "日常实习", "待确认"))
        ), None)
        if source and target:
            base = next((
                item for item in prepared
                if item.get("type") in {"create_application", "update_application"}
            ), {})
            merge_change = {
                "type": "merge_application",
                "source_application_id": source["id"],
                "target_application_id": target["id"],
                "company": company or target.get("company"),
                "role": role or target.get("role"),
                "status": base.get("status") or confirmed.get("status") or target.get("status"),
                "source": confirmed.get("source") or base.get("source") or target.get("source"),
                "notes": base.get("notes") or source.get("notes") or target.get("notes"),
                "remark_tag": "follow",
            }
            prepared = [
                item for item in prepared
                if item.get("type") not in {"create_application", "update_application"}
            ]
            prepared.append(merge_change)
            warnings.append(
                f"将把重复投递 #{source['id']} 合并进资料更完整的投递 #{target['id']}，并删除重复记录。"
            )

    prepared.extend(separated_followups)
    data["changes"] = prepared
    normalized_clarifications = _normalize_clarifications(clarifications)
    data["clarifications"] = [
        item for item in normalized_clarifications
        if not _clarification_is_resolved(item.get("question"), confirmed)
    ]
    data["warnings"] = list(dict.fromkeys(x for x in warnings if str(x).strip()))
    data["decisions"] = followup_decisions
    data["ready_to_apply"] = bool(prepared) and not data["clarifications"] and not followup_decisions
    project_updates = [x for x in prepared if x.get("type") == "update_project"]
    if project_updates and len(project_updates) == len(prepared):
        contexts = list(dict.fromkeys(
            x.get("display_context") for x in project_updates if x.get("display_context")
        ))
        scope = f"“{contexts[0]}”下的 " if len(contexts) == 1 else ""
        data["display_summary"] = (
            f"将更新{scope}{len(project_updates)} 个现有项目。"
            "不会新增重复项目；确认后会写入详细内容和对应追问。"
        )
    return data


@app.post("/api/decisions/followup-migration/resolve")
def resolve_followup_migration(body: dict):
    mode = str((body or {}).get("mode") or "").strip().lower()
    if mode not in {"move", "copy", "keep", "selected"}:
        raise HTTPException(400, "请选择明确的问答处理方式")
    project_change = dict((body or {}).get("project_change") or {})
    project_id = _positive_int(project_change.get("project_id"))
    project = db.get_project(project_id) if project_id else None
    if not project or project_change.get("type") != "update_project":
        raise HTTPException(400, "问答迁移目标项目无效")
    original_document = str(project_change.get("document") or "")
    clean_document, items = _split_numbered_project_followups(original_document)
    if not items:
        raise HTTPException(400, "候选文档中没有可处理的面试问答")
    selected_keys = {
        re.sub(r"\s+", "", str(value or "")).lower()
        for value in ((body or {}).get("selected_questions") or [])
        if str(value or "").strip()
    }
    selected = items if mode != "selected" else [
        item for item in items
        if re.sub(r"\s+", "", item.get("question") or "").lower() in selected_keys
    ]
    if mode == "selected" and not selected:
        raise HTTPException(400, "请至少勾选一组面试问答")
    resolved_change = dict(project_change)
    if mode == "move":
        resolved_change["document"] = clean_document
    elif mode == "selected":
        resolved_change["document"] = _remove_selected_project_followups(
            original_document,
            {re.sub(r"\s+", "", item.get("question") or "").lower() for item in selected},
        )
    changes = [resolved_change]
    if mode in {"move", "copy", "selected"}:
        changes.extend({
            "type": "create_followup", "project_id": project_id, **item,
        } for item in selected)
    return {
        "summary": {
            "move": "移入面试准备",
            "copy": "复制到面试准备",
            "keep": "继续留在主文档",
            "selected": "按选择移入面试准备",
        }[mode],
        "changes": changes,
        "selected_count": len(selected) if mode != "keep" else 0,
    }


@app.post("/api/chat/propose")
def propose(body: ProposeIn):
    history = db.get_chat_history(body.session_id, limit=100)
    if not history:
        return {"summary": "无", "ready_to_apply": False,
                "clarifications": [], "warnings": [], "changes": []}
    task_history = _proposal_task_window(history)
    if body.clarification_answers:
        structured_answers = []
        for item in body.clarification_answers[:20]:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            values = [str(value).strip() for value in (item.get("values") or []) if str(value).strip()]
            if question and values:
                structured_answers.append(f"{question}\n选择：{'；'.join(values)}")
        if structured_answers:
            task_history = list(task_history) + [{
                "id": None,
                "role": "user",
                "content": "写入前结构化确认（由用户点击选项直接提交）：\n\n" + "\n\n".join(structured_answers),
            }]
    convo = "\n".join(
        f"[消息 {m.get('id')}] {'用户' if m['role']=='user' else 'Caddie草稿'}：{m['content']}"
        for m in task_history
    )
    local_evidence, local_refs = _proposal_local_evidence(body.session_id, task_history)
    snapshot = db.snapshot_for_ai() + _proposal_inbox_catalog(task_history) + local_evidence
    prompt = PROPOSE_PROMPT % (snapshot, convo[:16000])
    try:
        # Archive extraction is a structured transformation task, not an
        # open-ended reasoning task. Instruction models return JSON more
        # reliably and with much lower latency.
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=2200,
                      timeout=25, profile_key="fast", allow_fallback=True)
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"整理失败（模型没返回合规 JSON）：{str(e)[:200]}")
    data.setdefault("summary", "")
    data.setdefault("changes", [])
    data = _prepare_proposed_changes(data, task_history)
    data["task_message_ids"] = [item.get("id") for item in task_history]
    data["evidence_files"] = local_refs
    return data


def _preflight_profile_changes(changes):
    """Validate every reference before the first database write."""
    errors = []
    seen_project_updates = set()
    supported = {
        "create_experience", "create_project", "update_project", "create_followup",
        "create_application", "update_application", "merge_application",
        "set_application_status", "add_interview",
    }
    for index, change in enumerate(changes or [], start=1):
        if not isinstance(change, dict):
            errors.append(f"第 {index} 项不是有效的结构化改动")
            continue
        kind = change.get("type")
        label = f"第 {index} 项 {kind or '未知操作'}"
        if kind not in supported:
            errors.append(f"{label}：不支持该操作类型")
            continue
        if kind == "create_experience":
            if not (change.get("company") or "").strip() or not (change.get("role") or "").strip():
                errors.append(f"{label}：公司和职位不能为空")
        elif kind == "create_project":
            if not db.get_experience(_positive_int(change.get("experience_id"))):
                errors.append(f"{label}：所属经历不存在")
            if not (change.get("name") or "").strip():
                errors.append(f"{label}：项目名称不能为空")
        elif kind == "update_project":
            project_id = _positive_int(change.get("project_id"))
            current = db.get_project(project_id) if project_id else None
            if not current:
                errors.append(f"{label}：项目 #{change.get('project_id')} 不存在")
            elif project_id in seen_project_updates:
                errors.append(f"{label}：项目 #{project_id} 在同一批次被重复更新")
            else:
                seen_project_updates.add(project_id)
            if change.get("reference_knowledge_id"):
                knowledge = db.get_knowledge_item(_positive_int(change.get("reference_knowledge_id")))
                if not knowledge or not (knowledge.get("content") or "").strip():
                    errors.append(f"{label}：参考知识文档不存在或正文为空")
                if not (change.get("document") or "").strip():
                    errors.append(f"{label}：参考知识文档时必须生成该项目独立的完整正文")
        elif kind == "create_followup":
            if not db.get_project(_positive_int(change.get("project_id"))):
                errors.append(f"{label}：追问所属项目不存在")
        elif kind in {"update_application", "set_application_status", "add_interview"}:
            application_id = _positive_int(change.get("application_id"))
            if not application_id or not db.get_application(application_id):
                errors.append(f"{label}：投递 #{change.get('application_id')} 不存在")
        elif kind == "merge_application":
            source_id = _positive_int(change.get("source_application_id"))
            target_id = _positive_int(change.get("target_application_id"))
            if not source_id or not db.get_application(source_id):
                errors.append(f"{label}：待合并投递 #{change.get('source_application_id')} 不存在")
            if not target_id or not db.get_application(target_id):
                errors.append(f"{label}：目标投递 #{change.get('target_application_id')} 不存在")
            if source_id and target_id and source_id == target_id:
                errors.append(f"{label}：不能把投递记录合并到自身")

        primary_source_id = _positive_int(change.get("primary_source_id"))
        if primary_source_id:
            source = db.get_source(primary_source_id)
            if not source or not (source.get("content") or "").strip():
                if db.get_knowledge_item(primary_source_id):
                    errors.append(
                        f"{label}：#{primary_source_id} 是知识文档，不是原始资料"
                    )
                else:
                    errors.append(f"{label}：原始资料 #{primary_source_id} 不存在或正文为空")
        for source_id in change.get("supporting_source_ids") or []:
            sid = _positive_int(source_id)
            if not sid or not db.get_source(sid):
                errors.append(f"{label}：辅助资料 #{source_id} 不存在")
    return list(dict.fromkeys(errors))


def _authorize_profile_status_corrections(changes, session_id):
    """Sign correction flags for old proposals using persisted user evidence."""
    prepared = [dict(item) for item in (changes or [])]
    if not session_id:
        return prepared
    history = db.get_chat_history(session_id, limit=100)
    facts = _confirmed_profile_facts(_proposal_task_window(history))
    if not facts.get("status_correction"):
        for item in prepared:
            item.pop("allow_status_correction", None)
        return prepared
    for item in prepared:
        if item.get("type") != "update_application":
            continue
        current = db.get_application(_positive_int(item.get("application_id"))) or {}
        if item.get("status") and item.get("status") != current.get("status"):
            item["allow_status_correction"] = True
    return prepared


@app.post("/api/changes/apply")
def apply_changes(body: ApplyIn):
    recovered_changes, recovery_notes = _recover_confirmed_project_documents(
        body.changes, body.session_id
    )
    recovered_changes = _authorize_profile_status_corrections(
        recovered_changes, body.session_id
    )
    try:
        changes, local_notes = _materialize_local_project_changes(recovered_changes)
    except ValueError as exc:
        return {
            "ok": False,
            "results": [f"（失败：本地资料 — {str(exc)[:160]}）"],
            "ids": [],
            "success_count": 0,
            "failed_count": 1,
            "regenerate_required": True,
        }
    preflight_errors = _preflight_profile_changes(changes)
    if preflight_errors:
        return {
            "ok": False,
            "results": [f"（预检阻断：{item}）" for item in preflight_errors],
            "ids": [],
            "success_count": 0,
            "failed_count": len(preflight_errors),
            "regenerate_required": True,
        }
    results, app_ids, targets = db.apply_changes(changes)
    results = recovery_notes + local_notes + results
    failures = [item for item in results if item.startswith("（失败：")]
    successes = [item for item in results if not item.startswith("（失败：")]
    if successes:
        _commit("整理入库：" + "；".join(successes)[:80])
        for target in targets:
            target_type = target.get("type") or "profile"
            target_id = target.get("id")
            _workspace_event(
                "profile_write_confirmed", target_type, target_id,
                f"确认写入：{target.get('label') or target_type}",
                "experience" if target_type in {"experience", "project"} else "global",
                target.get("parent_id") or (target_id if target_type == "experience" else None),
                payload={"session_id": body.session_id, "operation": "changes_apply"},
            )
    return {
        "ok": not failures,
        "results": results,
        "ids": app_ids,
        "targets": targets,
        "success_count": len(successes),
        "failed_count": len(failures),
        "regenerate_required": bool(failures),
    }


# ─── 版本管理（本地 Git，自动提交）──────────────────────────────────────────

@app.get("/api/vcs/log")
def vcs_log():
    return {"status": vcs.status_summary(), "log": vcs.log()}


@app.get("/api/vcs/show/{h}")
def vcs_show(h: str):
    return vcs.show(h)


@app.post("/api/vcs/push")
def vcs_push():
    vcs.push()
    return {"ok": True, "status": vcs.status_summary()}


@app.get("/api/projects/{pid}/history")
def project_history(pid: int):
    return vcs.project_history(pid)


@app.post("/api/projects/{pid}/restore")
def project_restore(pid: int, body: dict):
    cur = db.get_project(pid)
    if not cur:
        raise HTTPException(404, "项目不存在")
    old = vcs.project_doc_at(pid, body.get("hash", ""))
    if not old:
        raise HTTPException(400, "找不到该历史版本")
    db.update_project(pid, {
        "name": cur.get("name"),
        "one_liner": old.get("one_liner") or cur.get("one_liner"),
        "document": old.get("document"),
        "technologies": old.get("technologies") or cur.get("technologies"),
        "keywords": old.get("keywords") or cur.get("keywords"),
    })
    _commit(f"回滚项目「{cur.get('name','')}」到历史版本 {body.get('hash','')[:7]}")
    return {"ok": True}


# ─── 个人网页：按经历改，只做文本替换，不动风格 ──────────────────────────────

SITE_HTML = ai.CONFIG_DIR / "site.html"
SITE_META = ai.CONFIG_DIR / "site_meta.json"


class SiteLoadIn(BaseModel):
    source_type: str   # path | url | inline
    value: str


class SiteInstructIn(BaseModel):
    instruction: str = ""


class SiteApplyIn(BaseModel):
    edits: list


def _site_html() -> str:
    return SITE_HTML.read_text(encoding="utf-8") if SITE_HTML.exists() else ""


def _site_meta() -> dict:
    if SITE_META.exists():
        try:
            return json.loads(SITE_META.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


@app.get("/api/site")
def site_get():
    return {"html": _site_html(), "meta": _site_meta()}


@app.post("/api/site/load")
def site_load(body: SiteLoadIn):
    html = ""
    meta = {}
    if body.source_type == "path":
        raw = body.value.strip().strip('"').strip("'")
        p = Path(raw.replace("~", str(Path.home()), 1) if raw.startswith("~") else raw)
        if not p.exists():
            raise HTTPException(400, f"找不到文件：{p}")
        html = p.read_text(encoding="utf-8", errors="ignore")
        meta = {"source": str(p), "kind": "本地文件"}
    elif body.source_type == "url":
        try:
            r = requests.get(body.value.strip(), timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            html = r.text
        except Exception as e:
            raise HTTPException(400, f"抓取失败：{str(e)[:160]}")
        meta = {"source": body.value.strip(), "kind": "在线网址"}
    else:
        html = body.value
        meta = {"source": "上传的文件", "kind": "上传"}
    if len(html) < 20:
        raise HTTPException(400, "内容太短，可能没读到网页正文。")
    ai.CONFIG_DIR.mkdir(exist_ok=True)
    SITE_HTML.write_text(html, encoding="utf-8")
    meta["loaded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    meta["chars"] = len(html)
    SITE_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "chars": len(html), "meta": meta}


SITE_PROMPT = """你在帮用户改他的【个人作品集网页】。给你三样东西：当前网页完整 HTML、用户在 Caddie 里的经历/项目、用户这次的指令。
你要产出对网页的【精确文本替换】清单。铁律：
1. 绝对不要改 <style> 里的 CSS、配色、字体、布局结构。只改可见文本内容（标题、描述文字、项目条目、数字等）。
2. 每条替换的 find 必须是 HTML 里【一字不差、能唯一定位】的片段（连同标签），replace 是改后的片段。片段尽量短小精准。
3. 改动要基于用户在 Caddie 里的真实经历/项目（让网页和最新经历一致、更有说服力）。
4. 若用户指令确实需要动到风格/配色/布局，不要擅自改——在 summary 里说明，并把 style_changed 设为 true。
5. 【重要】不要因为 Caddie 里暂时没有某段经历，就删除网页上已有的真实内容。网页可能比 Caddie 更全。优先做补充和润色；replace 设为空（删除）要非常谨慎，且必须在 reason 里以「【删除】」开头说明理由。

当前网页 HTML：
%s

用户在 Caddie 的经历/项目：
%s

用户这次的指令（可能为空，为空则你根据经历找出最该更新的文本）：%s

只返回如下 JSON，不要多余文字：
{"summary":"这次改了什么（一句话）","style_changed":false,
 "edits":[{"find":"原文片段","replace":"改后片段","reason":"为什么这么改"}]}
若无需改动，edits 返回 []。"""


@app.post("/api/site/propose")
def site_propose(body: SiteInstructIn):
    html = _site_html()
    if not html:
        raise HTTPException(400, "还没载入网页，先在上方载入你的 HTML。")
    prompt = SITE_PROMPT % (html[:48000], db.snapshot_for_ai(), body.instruction or "（无具体指令）")
    try:
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=4000,
                      profile_key="writing")
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"生成建议失败（模型返回不合规）：{str(e)[:200]}")
    data.setdefault("edits", [])
    data.setdefault("summary", "")
    for e in data["edits"]:
        e["found"] = bool(e.get("find")) and (e["find"] in html)
    return data


def _inject_base(html: str, base_href: str) -> str:
    tag = f'<base href="{base_href}">'
    low = html.lower()
    i = low.find("<head")
    if i != -1:
        j = html.find(">", i)
        if j != -1:
            return html[:j + 1] + tag + html[j + 1:]
    return tag + html


@app.get("/api/site/raw", response_class=HTMLResponse)
def site_raw():
    """把载入的网页用真实地址提供出来，预览才能正确加载图片/字体/脚本。"""
    html = _site_html()
    if not html:
        return HTMLResponse("<p style='font-family:sans-serif;color:#888;padding:40px'>还没载入网页</p>")
    meta = _site_meta()
    kind = meta.get("kind")
    if kind == "本地文件":
        html = _inject_base(html, "/site-assets/")
    elif kind == "在线网址":
        html = _inject_base(html, meta.get("source", ""))
    return HTMLResponse(html)


@app.get("/site-assets/{path:path}")
def site_asset(path: str):
    """提供本地网页同目录下的资源（图片等）。"""
    meta = _site_meta()
    src = meta.get("source", "")
    if not src or meta.get("kind") != "本地文件":
        raise HTTPException(404)
    base = Path(src).parent.resolve()
    target = (base / path).resolve()
    if not str(target).startswith(str(base)) or not target.exists() or target.is_dir():
        raise HTTPException(404)
    return FileResponse(str(target))


@app.post("/api/site/apply")
def site_apply(body: SiteApplyIn):
    html = _site_html()
    if not html:
        raise HTTPException(400, "还没载入网页")
    applied, missed = [], []
    for e in body.edits:
        f, r = e.get("find"), e.get("replace", "")
        if f and f in html:
            html = html.replace(f, r, 1)
            applied.append(e.get("reason") or "(改动)")
        else:
            missed.append(e.get("reason") or (f or "")[:30])
    SITE_HTML.write_text(html, encoding="utf-8")
    meta = _site_meta()
    meta["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    SITE_META.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    _commit(f"更新个人网页（{len(applied)} 处）")
    return {"ok": True, "applied": applied, "missed": missed}


# ─── 项目级对话：在文档旁边和 AI 一起完善这个项目 ────────────────────────────

PROJECT_CHAT_BASE = """你在帮用户打磨【这一个具体项目】，目标：把它写成一段有说服力、能写进简历、经得起面试追问的项目经历。
铁律：你【不能】直接改文档。普通讨论只需要自然回答和追问，不要假装正在执行任务。
如果这轮对话出现了可以沉淀的信息，结尾可以用一行说明「可沉淀：背景 / 动作 / 结果 / 面试追问」，
并告诉用户需要时可以明确要求你整理进文档或生成追问；实际写入必须经过候选确认。用中文，简洁不啰嗦。"""

PROJECT_MODE = {
    "organize": "\n本轮采用【直接整理】模式：以帮用户组织和写为主，尽量少反问。用户给出信息后，"
                "直接把它组织成项目文档该有的语言，主动补全表达。只有在缺了关键信息、不问就没法写时，才问一个最关键的问题。",
    "ask": "\n本轮采用【追问深挖】模式：像资深面试官一样主动追问薄弱处——背景/你具体做了什么/量化结果/技术/难点，"
           "帮用户把模糊的地方问清楚。一次只问一两个，别一口气问太多。",
}


PORTFOLIO_CHAT_BASE = """你在帮用户打磨【这一个个人作品】（自己做的 app / side project / demo），目标：写成面试讲得清、能扛住追问的作品介绍。
铁律：你【不能】直接改文档。每轮结尾若出现可写入文档的信息，用一行说明你打算更新哪些（例如「✏️ 可更新：为什么做 + 技术实现」），然后提示用户点下方『🗂 整理进文档』确认。绝不假装已经改好了。用中文，简洁不啰嗦。
特别注意：面试官最爱追问个人作品的"为什么要单独做、跟现成工具(ChatGPT/Notion 等)的区别、到底解决什么问题、现在做到哪一步"——主动帮用户把这些讲清楚。"""


def _focus_block(scope: str = "portfolio") -> str:
    """把当前错题/考点库拼成可注入 prompt 的文本（自迭代内容层）。"""
    fps = db.list_focus_points(scope)
    if not fps:
        return ""
    lines = []
    for n in fps:
        tag = "【必答】" if (n.get("strength") or "").lower() == "hard" else "-"
        lines.append(f"{tag} {n.get('directive')}")
    return "【面试错题/考点（写这个作品时务必主动覆盖，尤其标【必答】的）】\n" + "\n".join(lines) + "\n"


def _project_context(p: dict) -> str:
    exp = p.get("experience") or {}
    evidence = []
    for src in _refresh_project_local_sources(p["id"]):
        if not src:
            continue
        evidence.append(
            f"### {src.get('title') or src.get('file_name') or '本地资料'}\n"
            f"{(src.get('content') or '')[:3000]}"
        )
    evidence_text = "\n\n".join(evidence[:8]) or "（暂无关联的本地事实资料）"
    fact_docs = interview_store.list_documents(
        document_type="project_fact", project_id=p["id"]
    )
    fact_docs_text = "\n\n".join(
        f"### {item.get('title') or '项目事实分区'}\n{(item.get('body') or '')[:5000]}"
        for item in fact_docs[:8]
    ) or "（暂无补充事实文档）"
    return f"""【正在完善的项目】
公司：{exp.get('company','')}　角色：{exp.get('role','')}
项目名：{p.get('name','')}
当前文档（Markdown）：
---
{p.get('document') or '（还是空的）'}
---
【关联的本地事实资料（从原文件读取的最新文本）】
{evidence_text}
【项目补充事实文档】
{fact_docs_text}"""


@app.get("/api/projects/{pid}/messages")
def project_messages(pid: int):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    return db.get_chat_history(f"proj-{pid}", limit=200)


@app.post("/api/projects/{pid}/messages")
def save_project_message(pid: int, body: ProjectMessageIn):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    message_id = db.save_message(f"proj-{pid}", body.role, body.content.strip())
    return {"id": message_id, "saved": True}


def _guard_project_chat_completion_claim(reply: str) -> str:
    """A conversational reply may discuss edits, but it must never claim a write occurred."""
    completion = re.compile(
        r"(?:已将|已经将|已经把|已把|已完成|已经完成|已合并|已经合并|已更新|已经更新|已写入|已经写入)"
    )
    write_target = re.compile(r"(?:项目|文档|追问|题库|资料|档案)")
    parts = re.split(r"(?<=[。！？\n])", reply or "")
    kept, removed = [], False
    for part in parts:
        if completion.search(part) and write_target.search(part):
            removed = True
            continue
        kept.append(part)
    if not removed:
        return reply
    remainder = "".join(kept).strip()
    status = (
        "状态说明：这轮仍是普通对话，没有修改任何项目资料。"
        "我可以把刚才的内容整理成候选；你明确说“合并到追问”或“合并到项目文档”后，"
        "右侧会出现待确认内容，只有你点“确认写入”才会真正保存。"
    )
    return status + (f"\n\n{remainder}" if remainder else "")


@app.post("/api/projects/{pid}/chat")
def project_chat(pid: int, body: ProjChatIn):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    sid = f"proj-{pid}"
    history = db.get_chat_history(sid, limit=16)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": body.message})
    if p.get("kind") == "personal":
        base = PORTFOLIO_CHAT_BASE + PROJECT_MODE.get(body.mode, PROJECT_MODE["organize"])
        fb = _focus_block()
        system = base + ("\n\n" + fb if fb else "") + "\n\n" + _project_context(p)
    else:
        system = PROJECT_CHAT_BASE + PROJECT_MODE.get(body.mode, PROJECT_MODE["organize"]) + "\n\n" + _project_context(p)
    try:
        reply = ai.chat(messages, system=system, max_tokens=1800, profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:300]}")
    reply = _guard_project_chat_completion_claim(reply)
    db.save_message(sid, "user", body.message)
    db.save_message(sid, "assistant", reply)
    return {"reply": reply}


PROJECT_PROPOSE_PROMPT = """你在帮用户完善一个项目。请只根据【对话中新增的信息】产出增量补丁，不要重写整篇文档。

硬规则：
1. 大段正文绝对不要放进 JSON。按下面的分隔符格式返回。
2. 项目文档只保留：标题、一句话亮点、背景、我的动作、结果。不要写“可能被追问/我的答法”小节。
3. 面试追问必须单独放到 FOLLOWUPS 区，后端会写入追问题库。
4. 如果某区没有新增信息，保留该区为空。
5. 叙述段可以自然成段；适合枚举的地方再用要点，不要强行全分点。

当前文档：
%s

对话：
%s

严格按此格式返回，不要 markdown 代码围栏，不要额外解释：
SUMMARY: 这次补充了什么（一句话）
ONE_LINER: 更新后的一句话亮点（没有就留空）
TECHNOLOGIES: 逗号分隔（没有就留空）
KEYWORDS: 逗号分隔（没有就留空）
===BACKGROUND===
只写新增/修正的背景段落
===ACTIONS===
只写新增/修正的“我的动作”
===RESULTS===
只写新增/修正的结果和量化影响
===FOLLOWUPS===
Q: 面试官可能追问的问题
A: 建议答法（没有就留空）
STATUS: todo/weak/stable
CATEGORY: 项目贡献/数据方法/业务理解/技术实现/结果量化/风险不足/其他
---
Q: 第二个问题
A:
STATUS: todo
CATEGORY: 其他
===END==="""


def _field_line(raw: str, name: str) -> str:
    m = re.search(rf"(?m)^{re.escape(name)}:\s*(.*)$", raw or "")
    return (m.group(1).strip() if m else "")


def _block(raw: str, name: str) -> str:
    pat = rf"(?s)==={re.escape(name)}===\s*(.*?)(?=\n===[A-Z_]+===|\Z)"
    m = re.search(pat, raw or "")
    return (m.group(1).strip() if m else "")


def _strip_followup_section(doc: str) -> str:
    if not doc:
        return ""
    headings = [
        r"可能被追问(?:的问题)?",
        r"高频追问(?:\s*&\s*我的答法)?",
        r"我的答法",
        r"面试追问",
        r"追问题库",
    ]
    pat = r"(?ms)^#{2,3}\s*(?:" + "|".join(headings) + r").*?(?=^#{2,3}\s+|\Z)"
    return re.sub(pat, "", doc).strip()


def _old_followup_section(doc: str) -> str:
    if not doc:
        return ""
    headings = [
        r"可能被追问(?:的问题)?",
        r"高频追问(?:\s*&\s*我的答法)?",
        r"我的答法",
        r"面试追问",
        r"追问题库",
    ]
    pat = r"(?ms)^#{2,3}\s*(?:" + "|".join(headings) + r").*?(?=^#{2,3}\s+|\Z)"
    m = re.search(pat, doc)
    return (m.group(0).strip() if m else "")


def _extract_followups_from_doc_text(section: str):
    if not section:
        return []
    text = re.sub(r"(?m)^#{2,3}\s+.*$", "", section).strip()
    chunks = re.split(r"\n(?=(?:[-*]\s+)?(?:Q[:：]|问[:：]|追问\s*\d*[:：]|\*\*追问|\d+[.、]\s*))", text)
    out = []
    for chunk in chunks:
        c = chunk.strip(" \n-*\t")
        if not c:
            continue
        c = re.sub(r"^\d+[.、]\s*", "", c).strip()
        q = ""
        a = ""
        m = re.search(r"(?:Q|问|追问\s*\d*)[:：]\s*(.+?)(?:\n|$)", c)
        if m:
            q = m.group(1).strip(" *")
            rest = c[m.end():].strip()
        else:
            lines = [x.strip(" -*") for x in c.splitlines() if x.strip()]
            q = lines[0] if lines else ""
            rest = "\n".join(lines[1:])
        am = re.search(r"(?:A|答|答法|我的答法)[:：]\s*(.*)", rest, flags=re.S)
        a = (am.group(1).strip() if am else rest).strip()
        q = re.sub(r"^\*\*|\*\*$", "", q).strip()
        if len(q) >= 4:
            out.append({"question": q[:300], "answer": a[:2000], "status": "todo", "category": None, "origin": "project_doc"})
    return out[:30]


def _section_body(doc: str, title: str) -> str:
    m = re.search(rf"(?ms)^##\s*{re.escape(title)}\s*\n(.*?)(?=^##\s+|\Z)", doc or "")
    return (m.group(1).strip() if m else "")


def _append_section(old: str, inc: str) -> str:
    old = (old or "").strip()
    inc = (inc or "").strip()
    if not inc:
        return old
    if inc in old:
        return old
    return (old + "\n\n" + inc).strip() if old else inc


def _merge_project_doc(p: dict, patch: dict) -> str:
    cur = _strip_followup_section(p.get("document") or "")
    title = p.get("name") or "项目"
    one_liner = patch.get("one_liner") or p.get("one_liner") or ""
    bg = _append_section(_section_body(cur, "背景"), patch.get("background"))
    act = _append_section(_section_body(cur, "我的动作") or _section_body(cur, "我做了什么"), patch.get("actions"))
    res = _append_section(_section_body(cur, "结果"), patch.get("results"))
    parts = [f"# {title}"]
    if one_liner:
        parts.append(f"> {one_liner}")
    parts.extend([
        "## 背景\n" + (bg or "（待补充）"),
        "## 我的动作\n" + (act or "（待补充）"),
        "## 结果\n" + (res or "（待补充）"),
    ])
    return "\n\n".join(parts).strip()


# 作品视角：每个视角=一套分段骨架（label, 区块KEY, 写作提示）
PORTFOLIO_LENSES = {
    "full": {"name": "全面展示", "desc": "完整呈现，适合个人网页/正式介绍", "sections": [
        ("这是什么", "WHAT", "解决什么问题、给谁用"),
        ("解决的问题", "WHY", "必要性、跟现成方案(ChatGPT/Notion等)的区别"),
        ("核心功能", "FEATURES", "它能做什么"),
        ("技术实现", "TECH", "架构、关键技术选型、难点"),
        ("亮点难点", "HIGHLIGHTS", "最值得说的、踩过的坑"),
        ("现状与规划", "STATUS", "做到哪一步了、下一步"),
    ]},
    "necessity": {"name": "产品必要性", "desc": "主打“为什么值得做”，扛必要性拷问", "sections": [
        ("这是什么", "WHAT", "解决什么问题、给谁用"),
        ("为什么做", "WHY", "必要性、跟现成方案的区别（面试官最爱问）"),
        ("怎么用", "HOW", "输入 · 处理 · 输出"),
        ("现状与规划", "STATUS", "做到哪一步了、下一步"),
    ]},
    "tech": {"name": "技术深度", "desc": "突出工程能力，重点写实现", "sections": [
        ("这是什么", "WHAT", "解决什么问题"),
        ("核心功能", "FEATURES", "它能做什么"),
        ("技术实现", "TECH", "架构、关键技术选型、难点（重点展开）"),
        ("现状与规划", "STATUS", "做到哪一步了、下一步"),
    ]},
}


def _portfolio_lens(lens: str):
    return PORTFOLIO_LENSES.get(lens) or PORTFOLIO_LENSES["full"]


def _portfolio_prompt(lens: str, focus_block: str, doc: str, convo: str) -> str:
    secs = _portfolio_lens(lens)["sections"]
    sec_fmt = "\n".join(f"==={key}===\n{label}：{hint}" for label, key, hint in secs)
    sec_names = "、".join(label for label, _, _ in secs)
    return f"""你在帮用户完善一个【个人作品】（自己做的 app / side project / demo）。只根据【对话中新增的信息】产出增量补丁，不要重写整篇。

{focus_block}
硬规则：
1. 大段正文不要塞进字段行，按下面分隔符格式返回。
2. 作品文档结构固定为：标题、一句话亮点、{sec_names}。
3. 务必主动覆盖上面【面试关注点】里的点（尤其标【必答】的），比如"为什么要单独做、跟现成工具的区别"。哪怕用户没主动说，也引导补全；实在没信息就在该段写「（待补：……）」。
4. 面试追问单独放到 FOLLOWUPS 区，后端会写入追问题库。
5. 某区没有新增信息就留空。

当前文档：
{doc}

对话：
{convo}

严格按此格式返回，不要 markdown 代码围栏，不要额外解释：
SUMMARY: 这次补充了什么（一句话）
ONE_LINER: 更新后的一句话亮点（没有就留空）
TECHNOLOGIES: 逗号分隔（没有就留空）
KEYWORDS: 逗号分隔（没有就留空）
{sec_fmt}
===FOLLOWUPS===
Q: 面试官可能追问的问题
A: 建议答法（没有就留空）
STATUS: todo/weak/stable
CATEGORY: 必要性/技术实现/产品判断/现状规划/其他
===END==="""


def _parse_portfolio_patch(raw: str, lens: str):
    secs = _portfolio_lens(lens)["sections"]
    data = {
        "summary": _field_line(raw, "SUMMARY"),
        "one_liner": _field_line(raw, "ONE_LINER"),
        "technologies": _field_line(raw, "TECHNOLOGIES"),
        "keywords": _field_line(raw, "KEYWORDS"),
        "followups": _parse_followups(raw),
        "_sections": [(label, _block(raw, key)) for label, key, _ in secs],
    }
    return data


def _strip_lead_heading(text: str) -> str:
    """剥掉区块内容自带的首行标题（避免和拼接的 ## 标题重复）。"""
    return re.sub(r"^\s*#{1,6}\s+\S.*\n+", "", (text or "").lstrip(), count=1).strip()


def _merge_portfolio_doc(p: dict, patch: dict) -> str:
    cur = _strip_followup_section(p.get("document") or "")
    title = p.get("name") or "作品"
    one_liner = patch.get("one_liner") or p.get("one_liner") or ""
    parts = [f"# {title}"]
    if one_liner:
        parts.append(f"> {one_liner}")
    lens_labels = [label for label, _ in patch.get("_sections", [])]
    for label, inc in patch.get("_sections", []):
        body = _append_section(_section_body(cur, label), _strip_lead_heading(inc))
        parts.append(f"## {label}\n" + (body or "（待补充）"))
    # 保留当前文档里不属于本视角的旧段落，绝不丢用户内容
    for m in re.finditer(r"(?ms)^##\s*(.+?)\s*\n(.*?)(?=^##\s+|\Z)", cur or ""):
        lbl, body = m.group(1).strip(), m.group(2).strip()
        if lbl not in lens_labels and body and body != "（待补充）":
            parts.append(f"## {lbl}\n" + body)
    return "\n\n".join(parts).strip()


def _parse_followups(raw: str):
    block = _block(raw, "FOLLOWUPS")
    if not block:
        return []
    out = []
    for chunk in re.split(r"\n---+\n", block):
        q = _field_line(chunk, "Q")
        if not q:
            continue
        status = (_field_line(chunk, "STATUS") or "todo").lower()
        if status not in ("stable", "weak", "todo"):
            status = "todo"
        out.append({
            "question": q,
            "answer": _field_line(chunk, "A"),
            "status": status,
            "category": _field_line(chunk, "CATEGORY") or None,
            "origin": "project_conversation",
        })
    return out


def _parse_project_patch(raw: str):
    return {
        "summary": _field_line(raw, "SUMMARY"),
        "one_liner": _field_line(raw, "ONE_LINER"),
        "technologies": _field_line(raw, "TECHNOLOGIES"),
        "keywords": _field_line(raw, "KEYWORDS"),
        "background": _block(raw, "BACKGROUND"),
        "actions": _block(raw, "ACTIONS"),
        "results": _block(raw, "RESULTS"),
        "followups": _parse_followups(raw),
    }


@app.get("/api/portfolio/lenses")
def portfolio_lenses():
    return {"items": [{"key": k, "name": v["name"], "desc": v["desc"],
                       "sections": [lbl for lbl, _, _ in v["sections"]]}
                      for k, v in PORTFOLIO_LENSES.items()]}


@app.post("/api/projects/{pid}/propose")
def project_propose(pid: int, lens: str = "full"):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    history = db.get_chat_history(f"proj-{pid}", limit=40)
    if not history:
        return {"summary": "还没聊过，先在右边和 Caddie 聊聊这个项目", "changes": []}
    convo = "\n".join(f"{'用户' if m['role']=='user' else 'Caddie'}：{m['content']}" for m in history)
    is_personal = p.get("kind") == "personal"
    if is_personal:
        prompt = _portfolio_prompt(lens, _focus_block(), p.get("document") or "（空）", convo[:8000])
    else:
        prompt = PROJECT_PROPOSE_PROMPT % (p.get("document") or "（空）", convo[:8000])
    try:
        raw = ai.chat([{"role": "user", "content": prompt}], max_tokens=3000,
                      profile_key="writing")
        data = _parse_portfolio_patch(raw, lens) if is_personal else _parse_project_patch(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"整理失败（模型返回无法解析）：{str(e)[:200]}")
    if is_personal:
        has_doc_patch = any((data.get(k) or "").strip() for k in ("one_liner", "technologies", "keywords")) \
            or any((inc or "").strip() for _, inc in data.get("_sections", []))
    else:
        has_doc_patch = any((data.get(k) or "").strip() for k in ("one_liner", "technologies", "keywords", "background", "actions", "results"))
    changes = []
    if has_doc_patch:
        changes.append({
            "type": "update_project", "project_id": pid, "name": p.get("name"),
            "one_liner": data.get("one_liner") or p.get("one_liner"),
            "technologies": data.get("technologies") or p.get("technologies"),
            "keywords": data.get("keywords") or p.get("keywords"),
            "document": _merge_portfolio_doc(p, data) if is_personal else _merge_project_doc(p, data),
            "reason": data.get("summary"),
        })
    for f in data.get("followups") or []:
        f.update({"type": "create_followup", "project_id": pid})
        changes.append(f)
    if not changes:
        return {"summary": data.get("summary") or "没有可更新的内容", "changes": []}
    return {"summary": data.get("summary", ""), "changes": changes}


@app.post("/api/projects/{pid}/extract-followups")
def extract_project_followups(pid: int, body: dict = None):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    doc = p.get("document") or ""
    new_doc, items = _split_numbered_project_followups(doc)
    if not items:
        section = _old_followup_section(doc)
        items = _extract_followups_from_doc_text(section)
        new_doc = _strip_followup_section(doc) if items else doc
    if not items:
        return {"summary": "没有在文档里识别到可拆出的追问小节", "changes": []}
    existing_questions = {
        re.sub(r"\s+", "", str(row.get("question") or "")).lower()
        for row in db.list_followups(project_id=pid)
    }
    items = [
        item for item in items
        if re.sub(r"\s+", "", item.get("question") or "").lower() not in existing_questions
    ]
    if not items:
        return {"summary": "识别到的追问已经存在于面试准备中", "changes": []}

    mode = str((body or {}).get("mode") or "").strip().lower()
    if not mode:
        return {
            "summary": f"检测到 {len(items)} 组面试问答",
            "decision_type": "followup_migration",
            "count": len(items),
            "project_id": pid,
            "project_name": p.get("name"),
            "target_path": f"我的经历 / {p.get('name') or '当前项目'} / 面试准备",
            "items": items,
            "changes": [],
        }
    if mode == "keep":
        return {"summary": "继续留在主文档，不创建题库记录", "changes": []}

    selected_questions = {
        re.sub(r"\s+", "", str(value or "")).lower()
        for value in ((body or {}).get("selected_questions") or [])
        if str(value or "").strip()
    }
    selected = items
    if mode == "selected":
        selected = [
            item for item in items
            if re.sub(r"\s+", "", item.get("question") or "").lower() in selected_questions
        ]
        if not selected:
            raise HTTPException(400, "请至少勾选一组面试问答")

    changes = []
    should_remove = mode in {"move", "selected"}
    if should_remove:
        target_document = new_doc if mode == "move" else _remove_selected_project_followups(
            doc, {
                re.sub(r"\s+", "", item.get("question") or "").lower()
                for item in selected
            },
        )
        changes.append({
            "type": "update_project",
            "project_id": pid,
            "name": p.get("name"),
            "one_liner": p.get("one_liner"),
            "technologies": p.get("technologies"),
            "keywords": p.get("keywords"),
            "document": target_document,
            "reason": "从核心项目事实中移除已归档的面试问答",
        })
    for item in selected:
        item.update({"type": "create_followup", "project_id": pid})
        changes.append(item)
    action_label = {
        "move": "移入面试准备",
        "copy": "复制到面试准备",
        "selected": "按选择加入面试准备",
    }.get(mode)
    if not action_label:
        raise HTTPException(400, "不支持的处理方式")
    return {
        "summary": f"{action_label}：{len(selected)} 组面试问答",
        "changes": changes,
        "selected_count": len(selected),
        "remove_from_document": should_remove,
    }


# ─── 文件文字提取（PDF / 纯文本通用）─────────────────────────────────────────

def _extract_text(raw: bytes, filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    if name.endswith(".docx"):
        import io
        import re
        import zipfile
        from xml.etree import ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        parts = []
        for node in root.iter():
            if node.tag == "{%s}t" % ns["w"] and node.text:
                parts.append(node.text)
            elif node.tag == "{%s}p" % ns["w"]:
                parts.append("\n")
        return re.sub(r"\n{3,}", "\n\n", "".join(parts))
    if name.endswith(".pptx"):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            slides = sorted(n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
            parts = []
            for slide in slides[:80]:
                root = ET.fromstring(z.read(slide))
                texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
                if texts:
                    parts.append("\n".join(t.strip() for t in texts if t and t.strip()))
            return "\n\n".join(parts)
    if name.endswith(".xlsx"):
        rows = _read_xlsx_rows(raw)
        return "\n".join("\t".join(c for c in row if c) for row in rows[:500])
    if name.endswith((".csv", ".tsv")):
        rows = _read_csv_rows(raw, filename)
        return "\n".join("\t".join(c for c in row if c) for row in rows[:1000])
    if name.endswith(".rtf"):
        text = raw.decode("utf-8", errors="ignore")
        text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
        text = re.sub(r"[{}]", " ", text)
        return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()
    return raw.decode("utf-8", errors="ignore")


def _refresh_linked_local_source(source_or_id):
    """Keep an Agent-readable snapshot in sync with an original local file."""
    src = db.get_source(source_or_id) if isinstance(source_or_id, int) else source_or_id
    if not src or src.get("origin") not in {"local_reference", "local_discovery"}:
        return src
    path = Path(src.get("file_path") or "").expanduser()
    if not path.exists() or not path.is_file():
        return src
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest == src.get("content_hash"):
        return src
    text = _extract_text(raw, path.name).strip()
    db.refresh_source_snapshot(src["id"], text, digest)
    return db.get_source(src["id"])


def _refresh_project_local_sources(project_id: int):
    refreshed = []
    for link in db.list_source_links(entity_type="project", entity_id=project_id):
        src = db.get_source(link.get("source_id"))
        if src:
            refreshed.append(_refresh_linked_local_source(src))
    return refreshed


# ─── 本地资料发现 Harness：扫描 → 候选池 → 确认导入 ───────────────────────

CAREER_FILE_EXTENSIONS = {".pdf", ".docx", ".md", ".txt", ".csv", ".tsv", ".xlsx", ".pptx", ".rtf"}
DISCOVERY_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".cache", "Library", "Applications", "System",
}
DISCOVERY_STATUS = {"candidate", "low_confidence", "duplicate", "imported", "ignored", "error"}

SIGNAL_RULES = {
    "resume": [
        "简历", "resume", "cv", "curriculum vitae", "教育背景", "实习经历", "项目经历",
        "技能与工具", "自我评价", "求职意向",
    ],
    "jd": [
        "jd", "job description", "职位描述", "岗位职责", "任职要求", "招聘", "实习生招聘",
        "校招", "社招", "工作职责", "工作地点", "投递邮箱", "应聘方式",
    ],
    "project": [
        "项目", "star", "复盘", "总结", "prd", "需求文档", "产品方案", "测试计划",
        "数据分析", "案例研究", "作品", "portfolio", "建模", "gmv", "自动驾驶",
    ],
    "feedback": [
        "面试", "逐字稿", "面经", "追问", "复试", "一面", "二面", "三面", "interview",
        "面试官", "回答", "建议", "复盘",
    ],
    "worklog": [
        "周报", "日报", "月报", "工作记录", "实习报告", "weekly", "daily", "report",
        "meeting notes", "会议纪要",
    ],
    "knowledge": [
        "课程", "笔记", "论文", "研究", "行研", "行业研究", "宏观", "策略", "固收",
        "ipo", "估值", "财务分析", "wind", "case", "presentation",
    ],
}


def _json_loads_safe(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_discovery_roots() -> list[dict]:
    home = Path.home()
    candidates = [
        home / "Desktop" / "实习",
        home / "Documents" / "滴滴实习",
        home / "Downloads" / "电子产品数据",
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
    ]
    out, seen = [], set()
    for p in candidates:
        try:
            rp = p.expanduser().resolve()
        except Exception:
            continue
        key = str(rp)
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": key, "name": p.name, "exists": rp.exists() and rp.is_dir()})
    return out


def _normalize_discovery_paths(paths: list[str]) -> list[Path]:
    roots = []
    for raw in paths or []:
        text = (raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser().resolve()
        if not path.exists():
            raise HTTPException(400, f"路径不存在：{path}")
        if not path.is_dir():
            raise HTTPException(400, f"只能扫描文件夹：{path}")
        if str(path) in {"/", str(Path.home())}:
            raise HTTPException(400, "为避免误扫全盘，请选择更具体的文件夹。")
        roots.append(path)
    if not roots:
        roots = [Path(x["path"]) for x in _default_discovery_roots() if x.get("exists")][:3]
    return roots


def _clean_text_sample(text: str, limit: int = 12000) -> str:
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text[:limit]


def _brief_from_text(text: str, fallback: str) -> str:
    lines = [re.sub(r"\s+", " ", x).strip(" -#•\t") for x in (text or "").splitlines()]
    lines = [x for x in lines if len(x) >= 8]
    if not lines:
        return fallback
    return "；".join(lines[:3])[:220]


def _title_from_file(path: Path, text: str) -> str:
    stem = path.stem.strip()
    for line in (text or "").splitlines()[:12]:
        clean = re.sub(r"^[#\-\s]+", "", line).strip()
        if 4 <= len(clean) <= 60 and not re.search(r"^(电话|邮箱|手机|地址)[:：]", clean):
            if any(k in clean.lower() for k in ("简历", "jd", "岗位", "项目", "面试", "周报", "ipo", "研究", "产品")):
                return clean
    return stem or path.name


def _career_signals(path: Path, sample_text: str) -> dict:
    path_blob = " ".join(path.parts[-5:]).lower()
    text_blob = (sample_text or "").lower()
    blob = f"{path_blob}\n{text_blob[:6000]}"
    scores = {}
    hits = {}
    for source_type, keywords in SIGNAL_RULES.items():
        score, found = 0.0, []
        for kw in keywords:
            k = kw.lower()
            if k in path_blob:
                score += 2.2
                found.append(kw)
            elif k in text_blob:
                score += 1.0
                found.append(kw)
        if source_type == "resume" and re.search(r"(gpa|cet-6|教育背景|实习经历|工作经历|荣誉|技能)", blob):
            score += 1.5
        if source_type == "jd" and re.search(r"(岗位职责|任职资格|职位要求|每周|到岗|投递)", blob):
            score += 2.0
        if source_type == "feedback" and re.search(r"(面试官|追问|自我介绍|为什么|项目介绍)", blob):
            score += 1.5
        if source_type == "project" and re.search(r"(背景|目标|动作|结果|指标|口径|方案|测试|结论)", blob):
            score += 1.2
        scores[source_type] = score
        hits[source_type] = list(dict.fromkeys(found))[:10]
    best_type, best_score = max(scores.items(), key=lambda x: x[1])
    ext_bonus = 0.12 if path.suffix.lower() in CAREER_FILE_EXTENSIONS else 0
    confidence = min(0.98, max(0.05, best_score / 9.0 + ext_bonus))
    if best_score < 1.2:
        best_type = "other"
    entities = {}
    m = re.search(r"(字节|腾讯|阿里|美团|滴滴|华兴|中金|中粮|泰康|新华|申万宏源|国泰海通|华夏基金|万事达卡)[\w\u4e00-\u9fa5\-· ]{0,20}", sample_text or "")
    if m:
        entities["company_hint"] = m.group(0).strip()
    role = re.search(r"(产品经理|产品策划|PMO|投行|承做|行业研究|固收|策略|资管|AI产品|商业分析|数据分析)[\w\u4e00-\u9fa5\-· ]{0,16}", sample_text or "")
    if role:
        entities["role_hint"] = role.group(0).strip()
    return {
        "source_type": best_type,
        "confidence": round(confidence, 3),
        "signals": {
            "score_by_type": {k: round(v, 2) for k, v in scores.items()},
            "matched_keywords": {k: v for k, v in hits.items() if v},
            "entities": entities,
        },
        "suggested_track": {
            "company": entities.get("company_hint", ""),
            "role": entities.get("role_hint", ""),
            "target": "",
        } if best_type == "jd" else {},
    }


LOCAL_DISCOVERY_ANALYZE_PROMPT = """你是 Caddie 的本地资料迁移助手。请判断这份文件是否和求职、实习、面试、项目经历、研究作品或职业知识有关。

返回严格 JSON，不要输出 Markdown：
{
  "source_type": "resume/jd/project/feedback/worklog/knowledge/other",
  "title": "适合在资料库展示的标题",
  "summary": "2-4 句话说明这份资料是什么、有什么价值",
  "confidence": 0.0,
  "tags": ["标签"],
  "key_points": ["可入库的事实或信息"],
  "suggested_track": {"company": "", "role": "", "target": ""},
  "risks": ["需要人工确认的点"]
}

文件名：%s
本地路径：%s
内容片段：
%s"""


def _analyze_discovery_candidate_with_ai(candidate: dict) -> dict:
    text = candidate.get("sample_text") or ""
    raw = ai.chat([{"role": "user", "content": LOCAL_DISCOVERY_ANALYZE_PROMPT % (
        candidate.get("file_name") or "",
        candidate.get("file_path") or "",
        text[:9000],
    )}], max_tokens=1400, profile_key="fast")
    data = ai.extract_json(raw)
    data.setdefault("source_type", candidate.get("source_type") or "other")
    data.setdefault("title", candidate.get("title") or candidate.get("file_name") or "未命名资料")
    data.setdefault("summary", candidate.get("summary") or "")
    data["confidence"] = max(0.0, min(1.0, float(data.get("confidence") or candidate.get("confidence") or 0)))
    return data


def _candidate_payload_from_path(path: Path, run_id: int, max_file_mb: int) -> dict:
    stat = path.stat()
    size = stat.st_size
    content_hash = _file_hash(path)
    duplicate = db.find_source_by_hash(content_hash)
    sample_text, error = "", None
    if size > max_file_mb * 1024 * 1024:
        error = f"文件超过 {max_file_mb}MB，暂未解析正文"
    else:
        try:
            sample_text = _clean_text_sample(_extract_text(path.read_bytes(), path.name))
        except Exception as exc:
            error = f"解析失败：{str(exc)[:180]}"
    signal = _career_signals(path, sample_text)
    title = _title_from_file(path, sample_text)
    summary = _brief_from_text(sample_text, title)
    status = "duplicate" if duplicate else ("low_confidence" if signal["confidence"] < 0.28 else "candidate")
    if error and not sample_text:
        status = "error"
    return {
        "run_id": run_id,
        "file_path": str(path),
        "file_name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "content_hash": content_hash,
        "sample_text": sample_text,
        "source_type": signal["source_type"],
        "title": title,
        "summary": summary,
        "confidence": signal["confidence"],
        "signals_json": json.dumps(signal["signals"], ensure_ascii=False),
        "analysis_json": None,
        "duplicate_source_id": duplicate.get("id") if duplicate else None,
        "suggested_track_json": json.dumps(signal["suggested_track"], ensure_ascii=False) if signal.get("suggested_track") else None,
        "status": status,
        "error": error,
    }


def _enrich_candidate(item: dict) -> dict:
    out = dict(item)
    out["signals"] = _json_loads_safe(out.get("signals_json"), {})
    out["analysis"] = _json_loads_safe(out.get("analysis_json"), None)
    out["suggested_track"] = _json_loads_safe(out.get("suggested_track_json"), {})
    out["type_label"] = SOURCE_TYPES.get(out.get("source_type"), out.get("source_type") or "其他")
    if out.get("duplicate_source_id"):
        dup = db.get_source(out["duplicate_source_id"])
        out["duplicate_source_title"] = dup.get("title") if dup else None
    return out


def _walk_discovery_files(roots: list[Path], exts: set[str], include_hidden: bool, max_files: int):
    seen, count = set(), 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in DISCOVERY_SKIP_DIRS]
                filenames = [f for f in filenames if not f.startswith(".")]
            else:
                dirnames[:] = [d for d in dirnames if d not in DISCOVERY_SKIP_DIRS]
            for name in filenames:
                path = current / name
                if path.suffix.lower() not in exts:
                    continue
                try:
                    rp = path.resolve()
                except Exception:
                    continue
                key = str(rp)
                if key in seen:
                    continue
                seen.add(key)
                count += 1
                if count > max_files:
                    return
                yield rp


def _resume_path(value: Optional[str], expected: str) -> Optional[Path]:
    if not (value or "").strip():
        return None
    path = Path(value.strip()).expanduser().resolve()
    allowed = {"docx": {".docx", ".doc"}, "pdf": {".pdf"}}[expected]
    if path.suffix.lower() not in allowed:
        raise HTTPException(400, f"{expected.upper()} 文件格式不正确")
    if not path.exists() or not path.is_file():
        raise HTTPException(400, f"找不到本地文件：{path}")
    return path


def _resume_file_meta(path: Optional[Path]) -> dict:
    if not path:
        return {"path": None, "hash": None, "modified_at": None}
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path),
        "hash": digest.hexdigest(),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _resume_version_payload(body: ResumeVersionIn, track_id: Optional[int] = None) -> dict:
    docx = _resume_path(body.docx_path, "docx")
    pdf = _resume_path(body.pdf_path, "pdf")
    if not docx and not pdf:
        raise HTTPException(400, "至少选择一个 Word 或 PDF 文件")
    # 同名文件放在同一目录时自动配对，减少重复选择。
    if docx and not pdf:
        companion = docx.with_suffix(".pdf")
        if companion.exists():
            pdf = companion.resolve()
    if pdf and not docx:
        for suffix in (".docx", ".doc"):
            companion = pdf.with_suffix(suffix)
            if companion.exists():
                docx = companion.resolve()
                break
    docx_meta, pdf_meta = _resume_file_meta(docx), _resume_file_meta(pdf)
    source = docx or pdf
    try:
        extracted = _extract_text(source.read_bytes(), source.name).strip() if source else ""
    except Exception:
        extracted = ""
    status = body.status or "editing"
    if status not in {"editing", "submitted", "archived"}:
        raise HTTPException(400, "简历状态不正确")
    return {
        "track_id": track_id,
        "version_name": body.version_name.strip(),
        "docx_path": docx_meta["path"],
        "pdf_path": pdf_meta["path"],
        "extracted_text": extracted,
        "status": status,
        "change_summary": (body.change_summary or "").strip(),
        "docx_hash": docx_meta["hash"],
        "pdf_hash": pdf_meta["hash"],
        "docx_modified_at": docx_meta["modified_at"],
        "pdf_modified_at": pdf_meta["modified_at"],
    }


def _enrich_resume_version(item: dict) -> dict:
    out = dict(item)
    for kind in ("docx", "pdf"):
        raw = out.get(f"{kind}_path")
        path = Path(raw).expanduser() if raw else None
        out[f"{kind}_exists"] = bool(path and path.exists() and path.is_file())
        out[f"{kind}_name"] = path.name if path else None
        if path and path.exists():
            out[f"{kind}_current_modified_at"] = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            out[f"{kind}_changed"] = out.get(f"{kind}_current_modified_at") != out.get(f"{kind}_modified_at")
        else:
            out[f"{kind}_current_modified_at"] = None
            out[f"{kind}_changed"] = False
    return out


def _knowledge_payload(body: KnowledgeItemIn) -> dict:
    scope = body.scope_type or "global"
    if scope not in {"global", "domain", "company", "track"}:
        raise HTTPException(400, "知识作用域不正确")
    mastery = body.mastery or "learning"
    if mastery not in {"new", "learning", "answerable", "mastered"}:
        raise HTTPException(400, "掌握状态不正确")
    domain = (body.domain_key or "").strip() or None
    company = (body.company or "").strip() or None
    track_id = body.track_id
    if scope == "domain" and not domain:
        raise HTTPException(400, "行业/职能知识需要填写所属领域")
    if scope == "company" and not company:
        raise HTTPException(400, "公司知识需要填写公司名称")
    if scope == "track":
        if not track_id or not db.get_job_track(track_id):
            raise HTTPException(400, "岗位专属知识需要关联具体岗位")
    folder = db.get_knowledge_folder(body.folder_id) if body.folder_id else None
    if body.folder_id and not folder:
        raise HTTPException(400, "知识目录不存在")
    if folder and folder.get("track_id") and folder.get("track_id") != track_id:
        raise HTTPException(400, "知识目录不属于当前岗位")
    return {
        "title": body.title.strip(),
        "content": (body.content or "").strip(),
        "scope_type": scope,
        "domain_key": domain if scope == "domain" else None,
        "company": company if scope == "company" else None,
        "track_id": track_id if scope == "track" else None,
        "topic": (body.topic or "").strip() or None,
        "mastery": mastery,
        "folder_id": body.folder_id,
        "status": body.status or "active",
    }


def _enrich_knowledge_item(item: dict) -> dict:
    out = dict(item)
    scope = out.get("scope_type") or "global"
    if scope == "domain":
        out["scope_label"] = out.get("domain_key") or "行业/职能"
    elif scope == "company":
        out["scope_label"] = out.get("company") or "公司"
    elif scope == "track":
        track = db.get_job_track(out.get("track_id")) if out.get("track_id") else None
        out["scope_label"] = f"{track.get('company','')} · {track.get('role','')}" if track else "岗位专属"
    else:
        out["scope_label"] = "个人通用"
    source_labels = {
        "manual": "手动创建", "legacy": "旧知识卡迁移", "gap": "岗位差距诊断",
        "debrief": "面试复盘", "source": "资料库", "ai": "AI 整理",
    }
    out["source_label"] = source_labels.get(out.get("source_type"), out.get("source_type") or "手动创建")
    out["source_title"] = None
    if out.get("source_type") == "gap" and out.get("source_ref_id"):
        gap = db.get_track_gap(out["source_ref_id"])
        out["source_title"] = gap.get("requirement") if gap else None
    elif out.get("source_type") == "debrief" and out.get("source_ref_id"):
        asset = db.get_asset(out["source_ref_id"])
        out["source_title"] = asset.get("title") if asset else None
    elif out.get("source_type") == "source" and out.get("source_ref_id"):
        source = db.get_source(out["source_ref_id"])
        out["source_title"] = source.get("title") if source else None
    return out


# ─── 资料资产中心：原始资料 / 求职线 / AI 识别建议 ───────────────────────────

SOURCE_TYPES = {
    "resume": "简历",
    "jd": "JD",
    "project": "项目素材",
    "feedback": "面试反馈",
    "worklog": "日记周报",
    "knowledge": "知识资料",
    "other": "其他",
}


@app.get("/api/sources")
def list_sources(source_type: Optional[str] = None):
    return {"items": db.list_sources(source_type), "types": SOURCE_TYPES}


@app.get("/api/sources/{sid}")
def get_source(sid: int):
    src = db.get_source(sid)
    if not src:
        raise HTTPException(404, "资料不存在")
    if src.get("analysis_json"):
        try:
            src["analysis"] = json.loads(src["analysis_json"])
        except Exception:
            src["analysis"] = None
    return src


@app.post("/api/sources")
def add_source(body: SourceIn):
    text = (body.content or "").strip()
    if len(text) < 5:
        raise HTTPException(400, "资料内容太短")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sid = db.create_source({
        "source_type": body.source_type or "other",
        "title": body.title or text[:30],
        "content": text,
        "content_hash": content_hash,
        "origin": "paste",
        "status": "raw",
    })
    _workspace_event("source_created", "source", sid, f"新增资料：{body.title or text[:20]}")
    _commit(f"新增资料：{body.title or text[:20]}")
    size_bucket = "under_1k" if len(text) < 1000 else "1k_10k" if len(text) < 10000 else "10k_plus"
    telemetry.log_event(
        "source_added",
        {"source_type": body.source_type or "other", "origin": "paste", "size_bucket": size_bucket},
        entity_type="source",
        entity_id=sid,
    )
    return {"id": sid}


@app.post("/api/sources/upload")
async def upload_source(file: UploadFile = File(...)):
    raw = await file.read()
    content_hash = hashlib.sha256(raw).hexdigest()
    try:
        text = _extract_text(raw, file.filename).strip()
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{str(e)[:200]}")
    if len(text) < 10:
        raise HTTPException(400, "没从文件里读到足够文字（可能是扫描版/图片型 PDF）。")
    store_dir = ai.CONFIG_DIR / "sources"
    store_dir.mkdir(exist_ok=True)
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{(file.filename or 'source').replace('/', '_')}"
    path = store_dir / safe_name
    path.write_bytes(raw)
    sid = db.create_source({
        "source_type": "other",
        "title": file.filename or "上传资料",
        "content": text,
        "file_name": file.filename,
        "file_path": str(path),
        "content_hash": content_hash,
        "origin": "upload",
        "status": "raw",
    })
    _workspace_event("source_created", "source", sid, f"上传资料：{file.filename or '未命名文件'}")
    _commit(f"上传资料：{file.filename or sid}")
    size_bucket = "under_1k" if len(text) < 1000 else "1k_10k" if len(text) < 10000 else "10k_plus"
    telemetry.log_event(
        "source_added",
        {"source_type": "other", "origin": "upload", "size_bucket": size_bucket},
        entity_type="source",
        entity_id=sid,
    )
    return {"id": sid, "chars": len(text)}


SOURCE_ANALYZE_PROMPT = """你是 Caddie 的资料资产整理器。请识别这份求职资料，并给出结构化入库建议。

可选 source_type 只能是：
- resume：简历
- jd：岗位 JD
- project：项目素材/周报/项目文档
- feedback：面试反馈/复盘
- worklog：日记/周报/工作记录
- knowledge：面经/课程笔记/知识资料
- other：其他

请只基于资料原文，不要编造。返回严格 JSON：
{
  "source_type": "jd",
  "title": "简短标题",
  "summary": "3-5 句话摘要",
  "tags": ["AI产品", "Agent"],
  "key_points": ["可复用事实/要求/反馈点"],
  "suggested_track": {"company": "可为空", "role": "可为空", "target": "可为空"},
  "suggested_links": [
    {"entity_type": "project/application/experience/interview_item/job_track", "name": "可能关联对象名称", "reason": "为什么相关"}
  ],
  "suggested_actions": [
    {"type": "create_job_track/update_project/create_knowledge_card/save_feedback/analyze_jd", "label": "用户能看懂的动作", "reason": "为什么建议做"}
  ],
  "risks": ["需要人工确认或可能不可靠的点"]
}

资料标题：%s
资料原文：
%s"""


@app.post("/api/sources/{sid}/analyze")
def analyze_source(sid: int):
    src = db.get_source(sid)
    if not src:
        raise HTTPException(404, "资料不存在")
    text = (src.get("content") or "").strip()
    if len(text) < 10:
        raise HTTPException(400, "资料内容太短")
    try:
        out = ai.chat([{"role": "user", "content": SOURCE_ANALYZE_PROMPT % (src.get("title") or "", text[:12000])}],
                      max_tokens=2200, profile_key="fast")
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"资料分析失败（模型返回不合规）：{str(e)[:200]}")
    data.setdefault("source_type", "other")
    data.setdefault("title", src.get("title") or "未命名资料")
    data.setdefault("summary", "")
    db.update_source_analysis(sid, {
        "source_type": data.get("source_type"),
        "title": data.get("title"),
        "summary": data.get("summary"),
        "analysis_json": json.dumps(data, ensure_ascii=False),
        "tags": ",".join(data.get("tags") or []),
        "confidence": data.get("confidence"),
        "status": "analyzed",
    })
    _commit(f"分析资料：{data.get('title')}")
    return data


@app.get("/api/local-discovery/default-roots")
def local_discovery_default_roots():
    return {"items": _default_discovery_roots(), "extensions": sorted(CAREER_FILE_EXTENSIONS)}


@app.post("/api/local-discovery/pick-folder")
def pick_local_discovery_folder():
    """Open the native folder picker used by the local desktop app."""
    if os.name == "nt":
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择要让 Caddie 扫描的文件夹")
            root.destroy()
        except Exception as exc:
            raise HTTPException(500, f"无法打开文件夹选择器：{str(exc)[:160]}")
        if not selected:
            raise HTTPException(400, "已取消选择")
        path = Path(selected).resolve()
        return {"path": str(path), "name": path.name or str(path)}
    if sys.platform != "darwin":
        raise HTTPException(400, "当前系统暂不支持系统文件夹选择器，请使用手动路径。")
    script = 'POSIX path of (choose folder with prompt "选择要让 Caddie 扫描的文件夹")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "选择文件夹超时")
    except Exception as exc:
        raise HTTPException(500, f"无法打开文件夹选择器：{str(exc)[:160]}")
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip()
        if "-128" in msg or "User canceled" in msg:
            raise HTTPException(400, "已取消选择")
        raise HTTPException(500, msg or "文件夹选择失败")
    path = Path((proc.stdout or "").strip()).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise HTTPException(400, f"选择的路径不是文件夹：{path}")
    return {"path": str(path), "name": path.name or str(path)}


@app.post("/api/local-discovery/pick-files")
def pick_local_source_files():
    """Select original files; Caddie records their paths instead of copying them."""
    if os.name == "nt":
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askopenfilenames(title="选择要关联到 Caddie 的原始文件")
            root.destroy()
        except Exception as exc:
            raise HTTPException(500, f"无法打开文件选择器：{str(exc)[:160]}")
        paths = [str(Path(value).resolve()) for value in selected]
    elif sys.platform == "darwin":
        script = """
set chosenFiles to choose file with prompt "选择要关联到 Caddie 的原始文件" with multiple selections allowed
set output to ""
repeat with chosenFile in chosenFiles
    set output to output & POSIX path of chosenFile & linefeed
end repeat
return output
"""
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(408, "选择文件超时")
        except Exception as exc:
            raise HTTPException(500, f"无法打开文件选择器：{str(exc)[:160]}")
        if proc.returncode != 0:
            msg = (proc.stderr or "").strip()
            if "-128" in msg or "User canceled" in msg:
                return {"paths": [], "cancelled": True}
            raise HTTPException(500, msg or "文件选择失败")
        paths = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    else:
        raise HTTPException(400, "当前系统暂不支持系统文件选择器。")
    valid = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if path.exists() and path.is_file():
            valid.append(str(path))
    return {"paths": valid, "cancelled": not bool(valid)}


@app.get("/api/local-discovery/runs")
def local_discovery_runs():
    return {"items": db.list_discovery_runs(), "stats": db.discovery_stats()}


@app.get("/api/local-discovery/runs/{run_id}")
def local_discovery_run(run_id: int):
    run = db.get_discovery_run(run_id)
    if not run:
        raise HTTPException(404, "扫描批次不存在")
    return {"run": run, "stats": db.discovery_stats(run_id)}


@app.post("/api/local-discovery/runs/{run_id}/cancel")
def cancel_local_discovery_run(run_id: int):
    run = db.get_discovery_run(run_id)
    if not run:
        raise HTTPException(404, "扫描批次不存在")
    if run.get("status") in {"completed", "failed", "cancelled"}:
        return {"run": run, "stats": db.discovery_stats(run_id)}
    db.update_discovery_run(run_id, status="cancelled",
        summary="用户已取消扫描", finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return {"run": db.get_discovery_run(run_id), "stats": db.discovery_stats(run_id)}


@app.get("/api/local-discovery/candidates")
def local_discovery_candidates(run_id: Optional[int] = None, status: Optional[str] = None, limit: int = 300):
    items = [_enrich_candidate(x) for x in db.list_file_candidates(run_id=run_id, status=status, limit=min(max(limit, 1), 1000))]
    return {"items": items, "stats": db.discovery_stats(run_id)}


@app.get("/api/local-discovery/candidates/{candidate_id}")
def local_discovery_candidate(candidate_id: int):
    item = db.get_file_candidate(candidate_id)
    if not item:
        raise HTTPException(404, "候选文件不存在")
    return _enrich_candidate(item)


def _perform_local_discovery_scan(rid: int, roots: list[Path], exts: set[str], include_hidden: bool,
                                  max_files: int, max_file_mb: int, use_ai: bool):
    seen = candidate_count = error_count = 0
    ai_reviewed = 0
    try:
        for path in _walk_discovery_files(roots, exts, include_hidden, max_files):
            current_run = db.get_discovery_run(rid)
            if current_run and current_run.get("status") == "cancelled":
                return
            seen += 1
            try:
                payload = _candidate_payload_from_path(path, rid, max_file_mb)
                cid = db.upsert_file_candidate(payload)
                if payload.get("status") in {"candidate", "duplicate", "low_confidence"}:
                    candidate_count += 1
                if payload.get("status") == "error":
                    error_count += 1
                if use_ai and ai_reviewed < 10 and payload.get("status") == "candidate" and (payload.get("confidence") or 0) >= 0.45:
                    item = db.get_file_candidate(cid)
                    try:
                        data = _analyze_discovery_candidate_with_ai(item)
                        db.update_file_candidate(cid,
                            source_type=data.get("source_type"),
                            title=data.get("title"),
                            summary=data.get("summary"),
                            confidence=data.get("confidence"),
                            analysis_json=json.dumps(data, ensure_ascii=False),
                            suggested_track_json=json.dumps(data.get("suggested_track") or {}, ensure_ascii=False))
                        ai_reviewed += 1
                    except Exception as exc:
                        db.update_file_candidate(cid, error=f"AI 复核失败：{str(exc)[:160]}")
            except Exception as exc:
                error_count += 1
                continue
            if seen == 1 or seen % 20 == 0:
                stats = db.discovery_stats(rid)
                db.update_discovery_run(rid,
                    total_seen=seen, candidate_count=sum(stats.values()),
                    imported_count=stats.get("imported", 0), ignored_count=stats.get("ignored", 0),
                    error_count=error_count + stats.get("error", 0),
                    summary=f"正在扫描：已检查 {seen} 个文件，发现 {sum(stats.values())} 个候选。")
        stats = db.discovery_stats(rid)
        summary = f"扫描 {seen} 个可读文件，发现 {sum(stats.values())} 个候选；其中可导入 {stats.get('candidate', 0)} 个、重复 {stats.get('duplicate', 0)} 个。"
        db.update_discovery_run(rid,
            status="completed", total_seen=seen, candidate_count=sum(stats.values()),
            imported_count=stats.get("imported", 0), ignored_count=stats.get("ignored", 0),
            error_count=error_count + stats.get("error", 0), summary=summary,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:
        db.update_discovery_run(rid, status="failed", total_seen=seen,
            error_count=error_count + 1, summary=f"扫描失败：{str(exc)[:200]}",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@app.post("/api/local-discovery/scan")
def scan_local_discovery(body: LocalDiscoveryScanIn, background_tasks: BackgroundTasks):
    roots = _normalize_discovery_paths(body.paths)
    exts = {x.lower().strip() for x in (body.include_extensions or []) if x.strip()}
    exts = {x if x.startswith(".") else f".{x}" for x in exts}
    exts = (exts & CAREER_FILE_EXTENSIONS) or CAREER_FILE_EXTENSIONS
    max_files = min(max(int(body.max_files or 800), 1), 20000)
    max_file_mb = min(max(int(body.max_file_mb or 25), 1), 200)
    rid = db.create_discovery_run({
        "roots_json": json.dumps([str(x) for x in roots], ensure_ascii=False),
        "options_json": json.dumps({
            "include_extensions": sorted(exts),
            "include_hidden": body.include_hidden,
            "max_files": max_files,
            "max_file_mb": max_file_mb,
            "use_ai": body.use_ai,
        }, ensure_ascii=False),
        "status": "running",
        "summary": f"扫描已开始：最多检查 {max_files} 个文件。你可以先看已发现候选，也可以随时取消。",
    })
    background_tasks.add_task(_perform_local_discovery_scan, rid, roots, exts,
        body.include_hidden, max_files, max_file_mb, body.use_ai)
    return {"run": db.get_discovery_run(rid), "stats": db.discovery_stats(rid), "background": True}


@app.put("/api/local-discovery/candidates/{candidate_id}")
def update_local_discovery_candidate(candidate_id: int, body: LocalDiscoveryUpdateIn):
    item = db.get_file_candidate(candidate_id)
    if not item:
        raise HTTPException(404, "候选文件不存在")
    patch = {}
    if body.source_type is not None:
        if body.source_type not in SOURCE_TYPES:
            raise HTTPException(400, "资料类型不正确")
        patch["source_type"] = body.source_type
    if body.title is not None:
        patch["title"] = body.title.strip()
    if body.summary is not None:
        patch["summary"] = body.summary.strip()
    if body.status is not None:
        if body.status not in DISCOVERY_STATUS:
            raise HTTPException(400, "候选状态不正确")
        patch["status"] = body.status
    db.update_file_candidate(candidate_id, **patch)
    return _enrich_candidate(db.get_file_candidate(candidate_id))


@app.post("/api/local-discovery/candidates/{candidate_id}/ignore")
def ignore_local_discovery_candidate(candidate_id: int):
    if not db.get_file_candidate(candidate_id):
        raise HTTPException(404, "候选文件不存在")
    db.update_file_candidate(candidate_id, status="ignored")
    return {"ok": True}


@app.post("/api/local-discovery/candidates/{candidate_id}/analyze")
def analyze_local_discovery_candidate(candidate_id: int):
    item = db.get_file_candidate(candidate_id)
    if not item:
        raise HTTPException(404, "候选文件不存在")
    if not (item.get("sample_text") or "").strip():
        raise HTTPException(400, "这份文件没有可供 AI 复核的文本片段")
    try:
        data = _analyze_discovery_candidate_with_ai(item)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"AI 复核失败：{str(e)[:200]}")
    db.update_file_candidate(candidate_id,
        source_type=data.get("source_type"),
        title=data.get("title"),
        summary=data.get("summary"),
        confidence=data.get("confidence"),
        analysis_json=json.dumps(data, ensure_ascii=False),
        suggested_track_json=json.dumps(data.get("suggested_track") or {}, ensure_ascii=False),
        status="candidate")
    return data


@app.post("/api/local-discovery/ingestion-plan")
def local_discovery_ingestion_plan(body: LocalDiscoveryImportIn):
    ids = list(dict.fromkeys(int(x) for x in (body.candidate_ids or []) if int(x) > 0))
    candidates = []
    missing = []
    for cid in ids:
        item = db.get_file_candidate(cid)
        if item:
            candidates.append(item)
        else:
            missing.append({"id": cid, "error": "候选不存在"})
    plan = harness.plan_source_ingestion(candidates)
    if missing:
        plan["missing"] = missing
        plan["report"]["issues"].append({
            "level": "warning",
            "code": "missing_candidates",
            "message": f"{len(missing)} 个候选不存在，已从计划中跳过。",
        })
    return plan


@app.post("/api/local-discovery/import")
def import_local_discovery_candidates(body: LocalDiscoveryImportIn):
    ids = list(dict.fromkeys(int(x) for x in (body.candidate_ids or []) if int(x) > 0))
    if not ids:
        raise HTTPException(400, "请选择要导入的候选文件")
    preflight = harness.plan_source_ingestion([x for x in (db.get_file_candidate(cid) for cid in ids) if x])
    if not preflight["report"]["passed"]:
        raise HTTPException(400, preflight)
    imported, skipped, errors = [], [], []
    for cid in ids:
        item = db.get_file_candidate(cid)
        if not item:
            errors.append({"id": cid, "error": "候选不存在"})
            continue
        if item.get("status") == "imported" and item.get("matched_source_id"):
            skipped.append({"id": cid, "reason": "已导入", "source_id": item.get("matched_source_id")})
            continue
        if item.get("duplicate_source_id"):
            db.update_file_candidate(cid, status="duplicate")
            skipped.append({"id": cid, "reason": "资料库已有相同内容", "source_id": item.get("duplicate_source_id")})
            continue
        path = Path(item.get("file_path") or "").expanduser()
        if not path.exists() or not path.is_file():
            db.update_file_candidate(cid, status="error", error="本地文件已不存在")
            errors.append({"id": cid, "error": "本地文件已不存在"})
            continue
        try:
            raw = path.read_bytes()
            text = _extract_text(raw, path.name).strip()
            if len(text) < 10:
                raise ValueError("没有读到足够文字")
            analysis = _json_loads_safe(item.get("analysis_json"), {}) or {}
            tags = analysis.get("tags") or []
            sid = db.create_source({
                "source_type": item.get("source_type") or "other",
                "title": item.get("title") or item.get("file_name") or path.name,
                "content": text,
                "file_name": item.get("file_name") or path.name,
                "file_path": str(path),
                "summary": item.get("summary"),
                "analysis_json": item.get("analysis_json"),
                "status": "analyzed" if item.get("analysis_json") else "raw",
                "origin": "local_discovery",
                "content_hash": item.get("content_hash") or hashlib.sha256(raw).hexdigest(),
                "tags": ",".join(tags) if isinstance(tags, list) else str(tags or ""),
                "confidence": item.get("confidence"),
                "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            db.update_file_candidate(cid, status="imported", matched_source_id=sid, error=None)
            imported.append({"id": cid, "source_id": sid, "title": item.get("title") or path.name})
        except Exception as exc:
            db.update_file_candidate(cid, status="error", error=str(exc)[:200])
            errors.append({"id": cid, "error": str(exc)[:200]})
    if imported:
        _commit(f"本地发现导入资料 {len(imported)} 份")
    return {"preflight": preflight, "imported": imported, "skipped": skipped, "errors": errors, "stats": db.discovery_stats()}


@app.delete("/api/sources/{sid}")
def del_source(sid: int):
    if not db.delete_source(sid):
        raise HTTPException(404, "资料不存在")
    _commit("删除资料")
    return {"ok": True}


@app.get("/api/job-tracks")
def list_job_tracks():
    return db.list_job_tracks()


@app.get("/api/tracks", deprecated=True)
@app.get("/api/job-lines", deprecated=True)
def list_job_tracks_legacy():
    return list_job_tracks()


@app.get("/api/applications/{aid}/track")
def get_application_track(aid: int):
    db.ensure_application_tracks()
    app_ = db.get_application(aid)
    if not app_:
        raise HTTPException(404, "投递不存在")
    if not app_.get("track_id"):
        raise HTTPException(500, "这条投递还没有生成求职线")
    return {"track_id": app_["track_id"]}


@app.post("/api/job-tracks")
def add_job_track(body: JobTrackIn):
    jid = db.create_job_track(body.dict())
    db.ensure_track_knowledge_folders(jid)
    _workspace_event("track_created", "job_track", jid, f"新增求职线：{body.company or ''} · {body.role or ''}", "track", jid)
    _commit(f"新增求职线：{body.company or ''} · {body.role or ''}")
    telemetry.log_event(
        "job_track_created",
        {"has_jd": bool(body.jd), "creation_source": "manual"},
        entity_type="job_track",
        entity_id=jid,
    )
    return {"id": jid}


@app.get("/api/job-tracks/{tid}")
def get_job_track_detail(tid: int):
    t = db.sync_track_from_applications(tid) or db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    gaps = db.list_track_gaps(tid)
    apps = [a for a in db.get_applications() if a.get("track_id") == tid]
    assets = db.list_assets(track_id=tid, limit=50)
    knowledge = [_enrich_knowledge_item(x) for x in db.list_knowledge_items(track_id=tid)]
    knowledge_folders = db.list_knowledge_folders(track_id=tid, scope_type="track")
    done = sum(1 for g in gaps if g.get("status") == "done")
    resume_versions = [_enrich_resume_version(x) for x in db.list_resume_versions(tid)]
    return {"track": t, "gaps": gaps, "applications": apps, "assets": assets,
            "resume_versions": resume_versions, "knowledge_items": knowledge,
            "submission_materials": db.list_submission_materials(tid),
            "knowledge_folders": knowledge_folders,
            "gap_done": done, "gap_total": len(gaps)}


@app.get("/api/job-tracks/{tid}/readiness")
def get_job_track_readiness(tid: int):
    t = db.sync_track_from_applications(tid) or db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    feedback = db.list_feedback_notes(scope="global", status="active")
    feedback += db.list_feedback_notes(scope="track", scope_id=tid, status="active")
    return harness.compute_track_readiness(
        t,
        gaps=db.list_track_gaps(tid),
        assets=db.list_assets(track_id=tid, limit=200),
        knowledge_items=db.list_knowledge_items(track_id=tid),
        resume_versions=db.list_resume_versions(tid),
        feedback=feedback,
    )


@app.get("/api/job-tracks/{tid}/resume-versions")
def list_track_resume_versions(tid: int):
    if not db.get_job_track(tid):
        raise HTTPException(404, "求职线不存在")
    return {"items": [_enrich_resume_version(x) for x in db.list_resume_versions(tid)]}


@app.post("/api/job-tracks/{tid}/resume-versions")
def add_track_resume_version(tid: int, body: ResumeVersionIn):
    if not db.get_job_track(tid):
        raise HTTPException(404, "求职线不存在")
    data = _resume_version_payload(body, tid)
    rid = db.create_resume_version(data)
    _workspace_event("resume_version_created", "resume_version", rid, f"新增简历版本：{body.version_name}", "track", tid)
    _commit(f"登记岗位简历：{body.version_name}")
    return {"item": _enrich_resume_version(db.get_resume_version(rid))}


@app.put("/api/resume-versions/{rid}")
def edit_resume_version(rid: int, body: ResumeVersionIn):
    current = db.get_resume_version(rid)
    if not current:
        raise HTTPException(404, "简历版本不存在")
    data = _resume_version_payload(body, current.get("track_id"))
    db.update_resume_version(rid, data)
    _workspace_event("resume_version_updated", "resume_version", rid, f"更新简历版本：{body.version_name}", "track", current.get("track_id"))
    _commit(f"更新岗位简历：{body.version_name}")
    return {"item": _enrich_resume_version(db.get_resume_version(rid))}


@app.delete("/api/resume-versions/{rid}")
def del_resume_version(rid: int):
    current = db.get_resume_version(rid)
    if not current:
        raise HTTPException(404, "简历版本不存在")
    db.delete_resume_version(rid)
    _commit(f"移除简历登记：{current.get('version_name') or rid}")
    return {"ok": True}


@app.get("/api/resume-versions/{rid}/preview")
def preview_resume_version(rid: int):
    item = db.get_resume_version(rid)
    if not item:
        raise HTTPException(404, "简历版本不存在")
    path = _resume_path(item.get("pdf_path"), "pdf")
    if not path:
        raise HTTPException(400, "这个版本还没有 PDF，无法保留原排版预览")
    return FileResponse(str(path), media_type="application/pdf", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}",
    })


@app.post("/api/resume-versions/{rid}/open")
def open_resume_version(rid: int, body: dict):
    item = db.get_resume_version(rid)
    if not item:
        raise HTTPException(404, "简历版本不存在")
    target = body.get("target") or "docx"
    if target == "folder":
        raw = item.get("docx_path") or item.get("pdf_path")
        path = Path(raw).expanduser() if raw else None
        if not path or not path.exists():
            raise HTTPException(400, "本地文件已经移动或删除")
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["open", "-R", str(path)])
    else:
        expected = "pdf" if target == "pdf" else "docx"
        path = _resume_path(item.get(f"{expected}_path"), expected)
        if not path:
            raise HTTPException(400, f"这个版本没有 {expected.upper()} 文件")
        opener = (body.get("opener") or "system").lower()
        if opener == "system":
            if os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.Popen(["open", str(path)])
        elif opener in {"word", "wps"}:
            if expected != "docx":
                raise HTTPException(400, "指定编辑器只用于打开 Word 文件")
            if os.name == "nt":
                command = "winword" if opener == "word" else "wps"
                subprocess.Popen(["cmd", "/c", "start", "", command, str(path)])
            elif sys.platform == "darwin":
                app_name = "Microsoft Word" if opener == "word" else "WPS Office"
                try:
                    subprocess.run(["open", "-a", app_name, str(path)], check=True)
                except subprocess.CalledProcessError as exc:
                    raise HTTPException(400, f"没有找到 {app_name}，请选择系统默认方式") from exc
            else:
                command = "libreoffice" if opener == "word" else "wps"
                try:
                    subprocess.Popen([command, str(path)])
                except FileNotFoundError as exc:
                    raise HTTPException(400, "没有找到所选编辑器，请使用系统默认方式") from exc
    return {"ok": True}


@app.post("/api/job-tracks/{tid}/submission-materials")
def add_submission_material(tid: int, body: SubmissionMaterialIn):
    if not db.get_job_track(tid):
        raise HTTPException(404, "岗位不存在")
    if body.application_id:
        application = db.get_application(body.application_id)
        if not application or application.get("track_id") != tid:
            raise HTTPException(400, "投递记录不属于当前岗位")
    mid = db.create_submission_material({"track_id": tid, **body.dict()})
    _commit(f"登记投递材料：{body.title}")
    return {"item": db.get_submission_material(mid)}


@app.put("/api/submission-materials/{mid}")
def edit_submission_material(mid: int, body: SubmissionMaterialIn):
    current = db.get_submission_material(mid)
    if not current:
        raise HTTPException(404, "投递材料不存在")
    db.update_submission_material(mid, body.dict())
    _commit(f"更新投递材料：{body.title}")
    return {"item": db.get_submission_material(mid)}


@app.delete("/api/submission-materials/{mid}")
def remove_submission_material(mid: int):
    current = db.get_submission_material(mid)
    if not current or not db.delete_submission_material(mid):
        raise HTTPException(404, "投递材料不存在")
    _commit(f"移除投递材料：{current.get('title') or mid}")
    return {"ok": True}


@app.post("/api/submission-materials/{mid}/open")
def open_submission_material(mid: int):
    item = db.get_submission_material(mid)
    if not item:
        raise HTTPException(404, "投递材料不存在")
    raw = item.get("file_path")
    path = Path(raw).expanduser() if raw else None
    if not path or not path.exists() or not path.is_file():
        raise HTTPException(400, "本地文件已经移动或删除")
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open", str(path)])
    return {"ok": True}


@app.get("/api/companies/{company}/submission-workspace")
def company_submission_workspace(company: str):
    name = company.strip()
    tracks = [x for x in db.list_job_tracks() if (x.get("company") or "").strip() == name]
    track_ids = {x["id"] for x in tracks}
    applications = [x for x in db.get_applications() if x.get("track_id") in track_ids]
    history_materials = []
    for track in tracks:
        history_materials.extend([
            {**item, "role": track.get("role")}
            for item in db.list_submission_materials(track["id"])
        ])
    return {
        "company": name,
        "materials": db.list_company_submission_materials(name),
        "tracks": tracks,
        "applications": applications,
        "history_materials": history_materials,
        "captures": db.list_company_application_captures(name),
        "module_catalog": db.company_application_module_catalog(name),
    }


@app.get("/api/application-materials/workspace")
def application_materials_workspace():
    tracks = db.list_job_tracks()
    captures = db.list_all_company_application_captures()
    names = {(x.get('company') or '').strip() for x in tracks}
    names.update((x.get('company') or '').strip() for x in captures)
    names.update(db.list_company_submission_material_companies())
    names.discard('')
    materials = []
    for name in sorted(names):
        materials.extend(db.list_company_submission_materials(name))
    catalog = db.company_application_module_catalog('')
    companies = []
    for name in sorted(names):
        company_captures = [x for x in captures if (x.get('company') or '').strip() == name]
        company_materials = [x for x in materials if (x.get('company') or '').strip() == name]
        company_tracks = [x for x in tracks if (x.get('company') or '').strip() == name]
        industry_text = ' '.join(str(x.get('company_industry') or x.get('target') or '') for x in company_tracks)
        type_text = ' '.join(str(x.get('company_type') or '') for x in company_tracks)
        combined = f"{name} {industry_text} {type_text}"
        if '银行' in combined: sector = '银行'
        elif any(x in combined.lower() for x in ('finance','financial','证券','基金','保险','资管','投行','券商')): sector = '金融'
        elif any(x in combined.lower() for x in ('internet','互联网','科技','ai','人工智能')): sector = '互联网/科技'
        else: sector = '其他'
        companies.append({
            'name': name, 'sector': sector, 'industry': industry_text,
            'capture_count': len(company_captures), 'material_count': len(company_materials),
            'module_count': sum(len(x.get('structure') or []) for x in company_captures),
            'track_count': len(company_tracks),
        })
    return {
        'companies': companies,
        'captures': captures,
        'materials': materials,
        'tracks': tracks,
        'sectors': catalog.get('sectors') or [],
        'summary': {
            'company_count': sum(1 for x in companies if x['capture_count'] or x['material_count']),
            'capture_count': len(captures),
            'module_count': sum(len(x.get('structure') or []) for x in captures),
            'material_count': len(materials),
        },
    }


@app.get("/api/browser-capture/companies")
def browser_capture_companies():
    tracks = db.list_job_tracks()
    companies = {}
    for track in tracks:
        name = (track.get('company') or '').strip()
        if name:
            companies.setdefault(name, []).append({'id': track['id'], 'role': track.get('role') or '岗位'})
    return {"companies": [{"name": name, "tracks": values} for name, values in sorted(companies.items())]}


@app.post("/api/browser-capture/forms")
def save_browser_capture(body: BrowserCaptureIn):
    _require_feature_available("application_materials")
    if body.track_id:
        track = db.get_job_track(body.track_id)
        if not track or (track.get('company') or '').strip() != body.company.strip():
            raise HTTPException(400, "岗位与公司不匹配")
    safe_sections = _sanitize_browser_capture_structure(body.structure)
    cid = db.create_company_application_capture({**body.dict(), 'structure': safe_sections})
    _commit(f"保存网页网申：{body.company} · {body.title or body.portal_host or ''}")
    return {"ok": True, "id": cid, "field_count": sum(len(x['fields']) for x in safe_sections)}


def _sanitize_browser_capture_structure(structure):
    safe_sections = []
    for section in (structure or [])[:80]:
        if not isinstance(section, dict): continue
        fields = []
        for field in (section.get('fields') or [])[:200]:
            if not isinstance(field, dict): continue
            fields.append({
                'label': str(field.get('label') or '')[:500],
                'value': str(field.get('value') or '')[:20000],
                'field_type': str(field.get('field_type') or 'text')[:60],
                'semantic_key': str(field.get('semantic_key') or 'other')[:100],
                'required': bool(field.get('required')),
            })
        if fields: safe_sections.append({
            'title': str(section.get('title') or '未分组')[:300],
            'archived': bool(section.get('archived')),
            'fields': fields,
        })
    return safe_sections


@app.put("/api/browser-captures/{cid}")
def edit_browser_capture(cid: int, body: BrowserCaptureUpdateIn):
    _require_feature_available("application_materials")
    current = db.get_company_application_capture(cid)
    if not current:
        raise HTTPException(404, "网页网申记录不存在")
    if body.track_id:
        track = db.get_job_track(body.track_id)
        if not track or (track.get('company') or '').strip() != (current.get('company') or '').strip():
            raise HTTPException(400, "岗位与公司不匹配")
    item = db.update_company_application_capture(cid, {
        'title': body.title or current.get('title'),
        'track_id': body.track_id,
        'structure': _sanitize_browser_capture_structure(body.structure),
    })
    _commit(f"整理网页网申：{current.get('company')} · {body.title or current.get('title') or ''}")
    return {"item": item}


@app.delete("/api/browser-captures/{cid}")
def remove_browser_capture(cid: int):
    _require_feature_available("application_materials")
    current = db.get_company_application_capture(cid)
    if not current:
        raise HTTPException(404, "网页网申记录不存在")
    if not db.delete_company_application_capture(cid):
        raise HTTPException(500, "删除失败")
    _commit(f"删除网页网申：{current.get('company')} · {current.get('title') or ''}")
    return {"ok": True}


@app.post("/api/companies/{company}/submission-materials")
def add_company_submission_material(company: str, body: CompanySubmissionMaterialIn):
    _require_feature_available("application_materials")
    name = company.strip()
    if not name:
        raise HTTPException(400, "公司名称不能为空")
    mid = db.create_company_submission_material({"company": name, **body.dict()})
    _commit(f"建立公司网申母版：{name} · {body.title}")
    return {"item": db.get_company_submission_material(mid)}


@app.put("/api/company-submission-materials/{mid}")
def edit_company_submission_material(mid: int, body: CompanySubmissionMaterialIn):
    _require_feature_available("application_materials")
    item = db.update_company_submission_material(mid, body.dict())
    if not item:
        raise HTTPException(404, "公司网申材料不存在")
    _commit(f"更新公司网申母版：{body.title}")
    return {"item": item}


@app.delete("/api/company-submission-materials/{mid}")
def remove_company_submission_material(mid: int):
    _require_feature_available("application_materials")
    current = db.get_company_submission_material(mid)
    if not current or not db.archive_company_submission_material(mid):
        raise HTTPException(404, "公司网申材料不存在")
    _commit(f"归档公司网申母版：{current.get('title') or mid}")
    return {"ok": True}


@app.post("/api/company-submission-materials/{mid}/usages")
def add_company_submission_usage(mid: int, body: CompanySubmissionUsageIn):
    _require_feature_available("application_materials")
    material = db.get_company_submission_material(mid)
    if not material:
        raise HTTPException(404, "公司网申材料不存在")
    if body.track_id:
        track = db.get_job_track(body.track_id)
        if not track or (track.get("company") or "").strip() != (material.get("company") or "").strip():
            raise HTTPException(400, "岗位不属于该公司")
    if body.application_id:
        application = db.get_application(body.application_id)
        if not application or (body.track_id and application.get("track_id") != body.track_id):
            raise HTTPException(400, "投递记录与岗位不匹配")
    item = db.create_company_submission_usage(mid, body.dict())
    _commit(f"登记网申使用版本：{material.get('company')} · {material.get('title')}")
    return {"item": item}


@app.post("/api/company-submission-materials/{mid}/upload")
async def upload_company_submission_file(mid: int, file: UploadFile = File(...)):
    _require_feature_available("application_materials")
    material = db.get_company_submission_material(mid)
    if not material:
        raise HTTPException(404, "公司网申材料不存在")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传文件为空")
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(400, "文件不能超过 30MB")
    original = Path(file.filename or "material.bin").name
    suffix = Path(original).suffix[:12]
    target_dir = ai.CONFIG_DIR / "company_submission_files"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{mid}-{int(time.time())}-{uuid.uuid4().hex[:8]}{suffix}"
    target.write_bytes(raw)
    db.update_company_submission_material(mid, {
        "file_path": str(target),
        "change_summary": f"上传新文件：{original}",
    })
    _commit(f"上传公司网申材料：{material.get('title')}")
    return {"ok": True, "file_path": str(target), "filename": original}


@app.post("/api/company-submission-materials/{mid}/open")
def open_company_submission_file(mid: int):
    item = db.get_company_submission_material(mid)
    if not item:
        raise HTTPException(404, "公司网申材料不存在")
    path = Path(item.get("file_path") or "").expanduser()
    if not path.exists() or not path.is_file():
        raise HTTPException(400, "当前文件不存在或已经移动")
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open", str(path)])
    return {"ok": True}


@app.post("/api/applications/{aid}/resume-version")
def link_application_resume(aid: int, body: dict):
    rid = body.get("resume_version_id")
    if not db.set_application_resume_version(aid, int(rid) if rid else None):
        raise HTTPException(400, "简历版本与投递记录不属于同一岗位")
    _commit("关联投递简历版本")
    return {"ok": True}


@app.post("/api/local-files/pick")
def pick_local_file(body: dict):
    kind = body.get("kind") or "docx"
    if kind not in {"docx", "pdf"}:
        raise HTTPException(400, "文件类型不支持")
    label = "Word 简历（.docx/.doc）" if kind == "docx" else "PDF 简历（.pdf）"
    if os.name == "nt":
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            patterns = [("Word 文档", "*.docx *.doc")] if kind == "docx" else [("PDF 文档", "*.pdf")]
            selected = filedialog.askopenfilename(title=f"选择{label}", filetypes=patterns)
            root.destroy()
        except Exception as exc:
            raise HTTPException(500, f"无法打开文件选择器：{str(exc)[:160]}")
        if not selected:
            raise HTTPException(400, "没有选择文件")
        path = _resume_path(selected, kind)
        return {"path": str(path), "name": path.name}
    if sys.platform != "darwin":
        raise HTTPException(400, "当前系统暂不支持系统文件选择器，请手动填写路径")
    script = f'POSIX path of (choose file with prompt "选择{label}")'
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True,
                                text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "选择文件超时")
    if result.returncode != 0:
        raise HTTPException(400, "没有选择文件")
    path = _resume_path(result.stdout.strip(), kind)
    return {"path": str(path), "name": path.name}


@app.get("/api/knowledge-items")
def list_knowledge_items(track_id: Optional[int] = None, scope_type: Optional[str] = None,
                         q: Optional[str] = None, company: Optional[str] = None,
                         folder_id: Optional[str] = None):
    items = db.list_knowledge_items(track_id=track_id, scope_type=scope_type, query=q,
                                    company=company, folder_id=folder_id)
    return {"items": [_enrich_knowledge_item(x) for x in items]}


@app.get("/api/knowledge-folders")
def list_knowledge_folders(track_id: Optional[int] = None, company: Optional[str] = None,
                           scope_type: Optional[str] = None):
    return {"items": db.list_knowledge_folders(track_id=track_id, company=company,
                                                scope_type=scope_type)}


@app.post("/api/knowledge-folders")
def add_knowledge_folder(body: KnowledgeFolderIn):
    if not body.name.strip():
        raise HTTPException(400, "目录名称不能为空")
    fid = db.create_knowledge_folder(body.dict())
    _commit(f"新增知识目录：{body.name.strip()}")
    return {"item": db.get_knowledge_folder(fid)}


@app.put("/api/knowledge-folders/{fid}")
def edit_knowledge_folder(fid: int, body: KnowledgeFolderIn):
    if not db.get_knowledge_folder(fid):
        raise HTTPException(404, "知识目录不存在")
    db.update_knowledge_folder(fid, body.dict())
    _commit(f"更新知识目录：{body.name.strip()}")
    return {"item": db.get_knowledge_folder(fid)}


@app.delete("/api/knowledge-folders/{fid}")
def del_knowledge_folder(fid: int):
    item = db.get_knowledge_folder(fid)
    if not item:
        raise HTTPException(404, "知识目录不存在")
    db.delete_knowledge_folder(fid)
    _commit(f"删除知识目录：{item.get('name') or fid}")
    return {"ok": True}


@app.post("/api/job-tracks/{tid}/knowledge-workspace")
def initialize_track_knowledge_workspace(tid: int):
    track = db.get_job_track(tid)
    if not track:
        raise HTTPException(404, "岗位不存在")
    folders, created = db.ensure_track_knowledge_folders(tid)
    if created:
        _commit(f"初始化岗位准备空间：{track.get('company') or ''} · {track.get('role') or ''}")
    return {"folders": folders, "created": len(created)}


@app.post("/api/company-knowledge-workspace")
def initialize_company_knowledge_workspace(body: dict):
    company = (body.get("company") or "").strip()
    if not company:
        raise HTTPException(400, "公司名称不能为空")
    folders, created = db.ensure_company_knowledge_folders(company)
    if created:
        _commit(f"初始化公司研究目录：{company}")
    return {"folders": folders, "created": len(created)}


@app.post("/api/knowledge-items")
def add_knowledge_item(body: KnowledgeItemIn):
    data = _knowledge_payload(body)
    kid = db.create_knowledge_item(data)
    _workspace_event("knowledge_created", "knowledge_item", kid, f"新增知识：{body.title}", data.get("scope_type") or "global", data.get("track_id"))
    _commit(f"新增知识：{body.title}")
    return {"item": _enrich_knowledge_item(db.get_knowledge_item(kid))}


@app.put("/api/knowledge-items/{kid}")
def edit_knowledge_item(kid: int, body: KnowledgeItemIn):
    if not db.get_knowledge_item(kid):
        raise HTTPException(404, "知识不存在")
    data = _knowledge_payload(body)
    db.update_knowledge_item(kid, data)
    _workspace_event("knowledge_updated", "knowledge_item", kid, f"用户更新知识：{body.title}", data.get("scope_type") or "global", data.get("track_id"))
    _commit(f"更新知识：{body.title}")
    return {"item": _enrich_knowledge_item(db.get_knowledge_item(kid))}


@app.delete("/api/knowledge-items/{kid}")
def del_knowledge_item(kid: int):
    item = db.get_knowledge_item(kid)
    if not item:
        raise HTTPException(404, "知识不存在")
    db.delete_knowledge_item(kid)
    _commit(f"删除知识：{item.get('title') or kid}")
    return {"ok": True}


@app.post("/api/knowledge-items/{kid}/promote")
def promote_knowledge_item(kid: int, body: dict):
    item = db.get_knowledge_item(kid)
    if not item:
        raise HTTPException(404, "知识不存在")
    track_id = body.get("track_id") or item.get("track_id")
    track = db.get_job_track(track_id) if track_id else None
    scope = body.get("scope_type")
    if scope == "company" and track:
        item.update({"scope_type": "company", "company": track.get("company"),
                     "domain_key": None, "track_id": None})
    elif scope == "domain" and track:
        item.update({"scope_type": "domain",
                     "domain_key": track.get("track_group") or track.get("company_industry") or track.get("target"),
                     "company": None, "track_id": None})
    elif scope == "global":
        item.update({"scope_type": "global", "company": None,
                     "domain_key": None, "track_id": None})
    else:
        raise HTTPException(400, "无法提升到这个作用域")
    db.update_knowledge_item(kid, item)
    _commit(f"提升知识复用范围：{item.get('title') or kid}")
    return {"item": _enrich_knowledge_item(db.get_knowledge_item(kid))}


# ─── 统一 Agent Runtime API ─────────────────────────────────────────────────

def _expert_public_payload(expert: dict | None):
    if not expert:
        return expert
    payload = {key: expert.get(key) for key in (
        "key", "name", "role", "description", "capabilities", "tools",
        "model_profile", "allowed_actions", "icon", "color", "status", "sort_order",
    )}
    expert_key = expert.get("key")
    payload["executable_actions"] = {
        "career_lead": ["answer", "create_asset"],
        "experience_detective": ["create_followup", "update_followup", "update_project_document"],
        "job_researcher": ["answer", "create_asset"],
        "resume_editor": ["create_asset"],
        "knowledge_coach": ["create_global_knowledge", "create_track_knowledge", "update_knowledge"],
        "pressure_interviewer": ["create_asset"],
        "review_analyst": ["create_asset"],
    }.get(expert_key, [])
    payload["available_tools"] = {
        "career_lead": ["context_builder", "hybrid_search", "agent_runtime"],
        "experience_detective": ["project_reader", "source_reader", "feedback_constraints"],
        "job_researcher": ["track_context", "source_reader", "hybrid_search"],
        "resume_editor": ["resume_versions", "project_reader", "track_context", "feedback_constraints"],
        "knowledge_coach": ["knowledge_search", "knowledge_editor", "context_builder"],
        "pressure_interviewer": ["track_context", "project_reader", "knowledge_search", "interview_history"],
        "review_analyst": ["transcript_reader", "interview_history", "project_reader", "knowledge_editor"],
    }.get(expert_key, [])
    payload["unavailable_tools"] = [
        tool for tool in (payload.get("tools") or [])
        if tool not in payload["available_tools"]
    ]
    return payload


def _expert_model_selection(expert: dict | None) -> dict:
    return ai.resolve_model_profile((expert or {}).get("model_profile") or "deep_reasoning")


def _agent_run_model_fields(selection: dict) -> dict:
    return {
        "model_profile": selection["profile_key"],
        "provider_id": selection["provider_id"],
        "model_name": selection["model"],
    }


def _record_agent_model(task_id: int, run_id: int, selection: dict):
    fallback = "（使用当前 AI 作为回退）" if selection.get("uses_fallback") else ""
    db.create_agent_event({
        "task_id": task_id, "run_id": run_id, "event_type": "model",
        "label": f"已选择{selection['profile_name']}模型",
        "detail": f"{selection['provider_name']} · {selection['model']}{fallback}",
        "status": "done",
        "payload_json": json.dumps({
            "model_profile": selection["profile_key"],
            "provider_id": selection["provider_id"], "model": selection["model"],
            "uses_fallback": selection.get("uses_fallback", False),
        }, ensure_ascii=False),
    })


def _record_agent_actual_model(task_id: int, run_id: int):
    actual = ai.get_last_call_info() or {}
    if not actual.get("provider_id"):
        return
    db.update_agent_run(
        run_id, provider_id=actual.get("provider_id"), model_name=actual.get("model")
    )
    if actual.get("fallback_position"):
        db.create_agent_event({
            "task_id": task_id, "run_id": run_id, "event_type": "model_fallback",
            "label": "已切换备用模型",
            "detail": f"主模型不可用，本轮改由 {actual.get('provider_name')} · {actual.get('model')} 完成",
            "status": "done",
            "payload_json": json.dumps(actual, ensure_ascii=False),
        })


def _record_harness_report(task_id: int, run_id: int | None, report: harness.HarnessReport):
    level = "done" if report.passed else ("failed" if any(x.level == "error" for x in report.issues) else "warning")
    detail = f"score={report.score}"
    if report.issues:
        detail += "；" + "；".join(x.message for x in report.issues[:3])
    db.create_agent_event({
        "task_id": task_id, "run_id": run_id, "event_type": "harness",
        "label": f"Harness 质量门：{report.stage}",
        "detail": detail, "status": level,
        "payload_json": harness.report_event_payload(report),
    })


def _harness_needs_auto_repair(report: harness.HarnessReport) -> bool:
    """Only hard failures spend another model call; warnings stay visible for review."""
    return any(issue.level == "error" for issue in report.issues)


def _guard_agent_task(task: dict, expert_key: str):
    report = harness.validate_task(task, expert_key)
    _record_harness_report(task["id"], None, report)
    if not report.passed:
        raise ValueError("Harness 拦截任务：" + "；".join(x.message for x in report.issues if x.level == "error"))
    return report


@app.get("/api/agent/experts")
def list_agent_experts():
    items = []
    for expert in db.list_agent_experts():
        payload = _expert_public_payload(expert)
        payload["available"] = expert.get("key") not in agent_runtime.DISABLED_EXPERT_KEYS
        if not payload["available"]:
            payload["availability_text"] = "尚未开放 · 敬请期待"
        items.append(payload)
    return {"items": items}


@app.get("/api/agent/experts/{expert_key}")
def get_agent_expert(expert_key: str):
    if expert_key in agent_runtime.DISABLED_EXPERT_KEYS:
        raise HTTPException(410, "简历 Agent 已暂时下线；简历文件的登记、预览和打开仍可正常使用")
    expert = db.get_agent_expert(expert_key)
    if not expert:
        raise HTTPException(404, "Agent 专家不存在")
    return _expert_public_payload(expert)


@app.post("/api/agent/coordinate")
def coordinate_agent(body: AgentCoordinateIn):
    if not body.instruction.strip():
        raise HTTPException(400, "请先说明要解决的问题")
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位不存在")
    route = agent_runtime.coordinate(
        body.instruction, object_type=body.object_type, object_id=body.object_id,
        track_id=body.track_id,
    )
    result = {
        "expert": _expert_public_payload(route["expert"]),
        "reason": route["reason"], "confidence": route["confidence"],
        "task_type": route["task_type"],
        "delegates": [_expert_public_payload(x) for x in route["delegates"]],
    }
    if not body.create_task:
        return result
    task_id = db.create_agent_task({
        "task_type": route["task_type"],
        "title": body.title or body.instruction.strip()[:48],
        "instruction": body.instruction.strip(),
        "object_type": body.object_type, "object_id": body.object_id,
        "track_id": body.track_id, "company": body.company,
        "priority": body.priority, "assigned_expert": route["expert_key"],
        "conversation_id": body.conversation_id, "status": "queued",
        "context_json": json.dumps({
            "routing_reason": route["reason"],
            "delegate_experts": [x.get("key") for x in route["delegates"]],
        }, ensure_ascii=False),
    })
    db.create_agent_event({
        "task_id": task_id, "event_type": "routed", "label": "主理人已完成分诊",
        "detail": f"已交给「{route['expert'].get('name')}」：{route['reason']}",
        "status": "done",
        "payload_json": json.dumps({"expert_key": route["expert_key"]}, ensure_ascii=False),
    })
    result["task"] = _agent_task_payload(db.get_agent_task(task_id))
    return result

def _agent_task_artifacts(task: dict) -> list[dict]:
    """Return verified, navigable outputs for an Agent task.

    New protocol writes an explicit proposal target when the user confirms it.
    A small compatibility fallback recognizes legacy summaries such as
    ``知识文档(ID:71)`` only after verifying that the document belongs to the
    same job track. This keeps historical task cards useful without guessing.
    """
    full_task = task if "changes" in task else (db.get_agent_task(task.get("id")) or task)
    task_id = full_task.get("id")
    track_id = full_task.get("track_id")
    artifacts, seen = [], set()

    def add(kind: str, item: dict | None, *, inferred: bool = False):
        if not item:
            return
        key = (kind, item.get("id"))
        if key in seen:
            return
        seen.add(key)
        artifacts.append({
            "type": kind, "id": item.get("id"), "title": item.get("title") or "未命名内容",
            "track_id": item.get("track_id") or track_id, "inferred": inferred,
        })

    # Confirmed proposals have the strongest possible linkage.
    for change in full_task.get("changes") or []:
        if change.get("status") != "applied" or not change.get("target_id"):
            continue
        action = change.get("action_type")
        if action in {"create_global_knowledge", "create_track_knowledge", "update_knowledge"}:
            item = db.get_knowledge_item(change["target_id"])
            if item and (not track_id or item.get("track_id") == track_id):
                add("knowledge_item", item)
        elif action == "create_asset":
            item = db.get_asset(change["target_id"])
            if item and (not track_id or item.get("track_id") == track_id):
                add("asset", item)

    # Drafts are intentionally separate from confirmed career facts, but should
    # still be easy to reopen from the task that produced them.
    for item in db.list_assets_for_agent_task(task_id):
        if not track_id or item.get("track_id") == track_id:
            add("asset", item)

    # Compatibility for older Agents that wrote a knowledge document directly
    # and only left its ID in the completion summary.
    summary = full_task.get("result_summary") or ""
    for match in re.finditer(r"(?:知识(?:准备)?文档|文档)\s*[（(]\s*(?:ID\s*[:：]?\s*)?(\d+)\s*[)）]", summary, re.I):
        item = db.get_knowledge_item(int(match.group(1)))
        if item and (not track_id or item.get("track_id") == track_id):
            add("knowledge_item", item, inferred=True)
    return artifacts


def _agent_task_payload(task: dict | None):
    if not task:
        return task
    result = dict(task)
    for key in ("context_json",):
        if key in result:
            try:
                result[key.removesuffix("_json")] = json.loads(result.get(key) or "{}")
            except (TypeError, json.JSONDecodeError):
                result[key.removesuffix("_json")] = {}
    result["artifacts"] = _agent_task_artifacts(task)
    if result.get("changes"):
        result["changes"] = [
            {**change, "review": _agent_change_review(change)}
            for change in result["changes"]
        ]
    return result


def _agent_clip(value, limit):
    text = (value or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...（已截断）"


def _agent_change_review(change: dict) -> dict:
    """Create an explainable review payload without mutating the pending proposal."""
    metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
    action = change.get("action_type") or ""
    review = {
        "kind": "candidate", "evidence": metadata.get("evidence") or [],
        "edited": False, "conflict": False, "current_excerpt": None, "diff": change.get("diff"),
    }
    if action == "create_track_knowledge":
        track = db.get_job_track(metadata.get("track_id") or change.get("parent_id"))
        folder = db.get_knowledge_folder(metadata.get("folder_id")) if metadata.get("folder_id") else None
        valid_folder = bool(folder and folder.get("scope_type") == "track" and
                            folder.get("track_id") == (track or {}).get("id"))
        destination = f"{(track or {}).get('company') or ''} · {(track or {}).get('role') or (track or {}).get('target') or ''} / {metadata.get('folder_name') or '岗位准备'}".strip(" ·/")
        if not track or not valid_folder:
            return {**review, "kind": "create_document", "missing_target": True,
                    "destination": destination, "message": "岗位或目标准备目录已不存在，不能确认此候选。"}
        return {**review, "kind": "create_document", "destination": destination,
                "message": "确认后会创建一篇岗位专属准备文档。"}
    if action == "update_followup":
        current = db.get_followup(change.get("target_id"))
        if not current:
            return {**review, "kind": "followup", "missing_target": True,
                    "message": "原追问已不存在，不能确认此候选。"}
        current_answer = current.get("answer") or ""
        base_hash = metadata.get("base_answer_hash")
        current_hash = hashlib.sha256(current_answer.encode("utf-8")).hexdigest()
        return {
            **review, "kind": "followup", "current_title": current.get("question"),
            "current_excerpt": _agent_clip(current_answer or "（尚无回答）", 1800),
            "conflict": bool(base_hash and current_hash != base_hash),
            "message": "原追问回答已在候选生成后被修改，需要重新生成。"
                       if base_hash and current_hash != base_hash else "候选会更新这条追问的参考回答。",
        }
    if action not in {"update_knowledge", "update_project_document"}:
        return review
    current = (db.get_knowledge_item(change.get("target_id")) if action == "update_knowledge"
               else db.get_project(change.get("target_id")))
    if not current:
        return {**review, "kind": "document", "missing_target": True,
                "message": "原始文档已不存在，不能确认此候选。"}
    current_content = (current.get("content") if action == "update_knowledge" else current.get("document")) or ""
    current_title = current.get("title") if action == "update_knowledge" else current.get("name")
    base_hash = metadata.get("base_hash")
    current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
    generated_diff = "\n".join(difflib.unified_diff(
        current_content.splitlines(), (change.get("proposed_content") or "").splitlines(),
        fromfile="当前版本", tofile="Agent 候选", lineterm="",
    ))
    return {
        **review, "kind": "document", "current_title": current_title,
        "destination": (
            _knowledge_write_receipt(current, "update")["logical_path"]
            if action == "update_knowledge" else f"项目 / {current_title}"
        ),
        "current_excerpt": _agent_clip(current_content, 1800),
        "diff": _agent_clip(generated_diff or "（正文无变化）", 12000),
        "conflict": bool(base_hash and current_hash != base_hash),
        "message": "原文已在 Agent 生成后被修改，需要基于最新版重新生成。"
                   if base_hash and current_hash != base_hash else "候选基于当前原文版本生成。",
    }


def _agent_update_package_payload(task_id: int):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    payload = _agent_task_payload(task)
    payload["changes"] = [{**item, "review": _agent_change_review(item)} for item in (task.get("changes") or [])]
    return {
        "task": payload,
        "draft_assets": db.list_assets_for_agent_task(task_id),
        "change_cursor": db.latest_workspace_change_cursor(),
    }


@app.get("/api/agent/tasks")
def list_agent_tasks(status: Optional[str] = None, object_type: Optional[str] = None,
                     object_id: Optional[int] = None, track_id: Optional[int] = None,
                     limit: int = 50):
    return {"items": [_agent_task_payload(x) for x in db.list_agent_tasks(
        status=status, object_type=object_type, object_id=object_id,
        track_id=track_id, limit=limit)]}


@app.post("/api/agent/tasks")
def create_agent_task(body: AgentTaskIn):
    if not body.title.strip() or not body.instruction.strip():
        raise HTTPException(400, "任务标题和目标不能为空")
    if body.object_type == "resume_version" or body.assigned_expert in agent_runtime.DISABLED_EXPERT_KEYS:
        raise HTTPException(410, "简历 Agent 已暂时下线；当前仅保留简历文件和版本管理")
    values = body.dict(exclude={"context"})
    route = None
    if not body.assigned_expert:
        route = agent_runtime.coordinate(
            body.instruction, object_type=body.object_type,
            object_id=body.object_id, track_id=body.track_id,
        )
        values["assigned_expert"] = route["expert_key"]
        if values.get("task_type") == "general":
            values["task_type"] = route["task_type"]
    elif not db.get_agent_expert(body.assigned_expert):
        raise HTTPException(400, "指定的 Agent 专家不存在")
    task_id = db.create_agent_task({
        **values,
        "context_json": json.dumps(body.context or {}, ensure_ascii=False),
        "status": "queued",
    })
    db.create_agent_event({
        "task_id": task_id, "event_type": "routed", "label": "主理人已完成分诊",
        "detail": (f"已交给「{route['expert'].get('name')}」：{route['reason']}" if route
                   else f"已交给「{db.get_agent_expert(values['assigned_expert']).get('name')}」"),
        "status": "done",
    })
    return _agent_task_payload(db.get_agent_task(task_id))


@app.get("/api/agent/tasks/{task_id}")
def get_agent_task(task_id: int):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    return _agent_task_payload(task)


@app.get("/api/agent/tasks/{task_id}/package")
def get_agent_update_package(task_id: int):
    """A review-oriented package for one external Agent run.

    It intentionally keeps drafts separate from pending factual/feedback changes,
    so the UI never makes a generated draft look like confirmed personal data.
    """
    return _agent_update_package_payload(task_id)


def _external_run_for_task(task_id: int, agent_key: str = "external_agent") -> int:
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    if task.get("status") in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "已结束的任务不能继续提交候选修改")
    for run in reversed(task.get("runs") or []):
        if run.get("run_type") == "external" and run.get("status") in {"queued", "running", "ready"}:
            return run["id"]
    return db.create_agent_run({
        "task_id": task_id, "run_type": "external", "expert_key": agent_key,
        "model_profile": "external", "status": "running",
        "actor_type": "external_agent", "actor_key": agent_key,
    })


@app.post("/api/agent/workspace-bootstrap")
def agent_workspace_bootstrap(body: AgentWorkspaceBootstrapIn):
    """Return the minimum shared contract an external Agent needs before working."""
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位求职线不存在")
    if body.project_id and not db.get_project(body.project_id):
        raise HTTPException(404, "项目不存在")
    track = db.get_job_track(body.track_id) if body.track_id else None
    project = db.get_project(body.project_id) if body.project_id else None
    return {
        "workspace": {"product": "Caddie", "mode": "local_career_workspace", "change_cursor": db.latest_workspace_change_cursor()},
        "agent": {"key": body.agent_key, "write_policy": "draft_or_propose_only"},
        "selected": {"track": track, "project": project},
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
            "先读取最小必要上下文；不要默认读取全部职业资料。",
            "事实、数字、贡献边界和反馈约束只能提交候选，不能直接确认。",
            "用户直接编辑优先；发现更新后应通过变更游标重新读取相关对象。",
            "草稿资产可以保存，但必须标记为 draft 并保留 Agent 来源。",
            "用户指定岗位准备目录时，必须提交岗位知识文档候选，不能用通用草稿资产替代。",
        ],
        "quick_start": [
            "先调用 get_workspace_map，解析岗位、项目、文档和稳定写入位置。",
            "按任务意图读取 read_context_package，不要一次读取全部资料。",
            "内容写回用 save_career_document；岗位状态和面试排期分别用 "
            "propose_job_record_update、propose_interview_change。",
            "向用户说明当前结果是草稿还是待确认候选。",
            "后续继续工作前，用变更游标检查用户是否修改了相关内容。",
        ],
        "task_hint": body.task_hint or "",
    }


def _agent_integration_paths(home: Path | None = None) -> dict:
    """Resolve client config locations in one place so install and status never drift."""
    root = home or Path.home()
    project_dir = Path(__file__).resolve().parent
    frozen = bool(getattr(sys, "frozen", False))
    installed_mcp = root / ".caddie" / "bin" / "caddie-mcp"
    installed_cli = root / ".caddie" / "bin" / "caddie-agent"
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or (root / "AppData" / "Roaming"))
        claude_config = appdata / "Claude" / "claude_desktop_config.json"
    else:
        claude_config = root / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if frozen:
        from agent_launchers import ensure_packaged_launchers
        launchers = ensure_packaged_launchers(root, Path(sys.executable))
        mcp_command = launchers["mcp"]
        cli_script = launchers["cli"]
        mcp_args = []
    elif installed_mcp.exists() and installed_cli.exists():
        # A source-mode background service may survive an upgrade. Once the
        # packaged runtime has installed stable launchers, never write the old
        # project path back into Agent client configurations.
        mcp_command = installed_mcp
        cli_script = installed_cli
        mcp_args = []
    else:
        mcp_command = project_dir / "scripts" / "caddie-mcp.sh"
        cli_script = project_dir / "scripts" / "caddie-agent.sh"
        mcp_args = []
    return {
        "mcp_script": project_dir / "scripts" / "caddie-mcp.sh",
        "cli_script": cli_script,
        "mcp_command": mcp_command,
        "mcp_args": mcp_args,
        "packaged": frozen or mcp_command == installed_mcp,
        "codex": root / ".codex" / "config.toml",
        "claude_desktop": claude_config,
        "workbuddy": root / ".workbuddy" / "mcp.json",
    }


def _read_local_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _codex_mcp_snippet(command: Path, args: list[str], agent_key: str = "codex") -> str:
    args_text = json.dumps(args, ensure_ascii=False)
    return (
        f'[mcp_servers.caddie]\ncommand = "{command}"\nargs = {args_text}\n'
        f'env = {{ CADDIE_AGENT_KEY = "{agent_key}" }}\n'
    )


def _claude_mcp_snippet(command: Path, args: list[str], agent_key: str = "external_agent") -> str:
    return json.dumps(
        {"mcpServers": {"caddie": {
            "command": str(command), "args": args,
            "env": {"CADDIE_AGENT_KEY": agent_key},
        }}},
        ensure_ascii=False,
        indent=2,
    )


def _codex_is_configured(path: Path, command: Path, args: list[str]) -> bool:
    text = _read_local_text(path)
    return str(command) in text and "[mcp_servers.caddie]" in text and all(arg in text for arg in args)


def _claude_is_configured(path: Path, command: Path, args: list[str]) -> bool:
    try:
        value = json.loads(_read_local_text(path) or "{}")
    except json.JSONDecodeError:
        return False
    config = value.get("mcpServers", {}).get("caddie", {})
    return config.get("command") == str(command) and config.get("args", []) == args


def _json_mcp_is_configured(path: Path, command: Path, args: list[str]) -> bool:
    """Check clients such as WorkBuddy that use the standard mcpServers JSON shape."""
    return _claude_is_configured(path, command, args)


def _integration_backup_and_write(path: Path, content: str) -> str | None:
    """Preserve the full previous config before changing an external Agent's file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.caddie-backup-{stamp}")
        backup.write_text(_read_local_text(path), encoding="utf-8")
        backup_path = str(backup)
    temp = path.with_name(path.name + ".caddie-tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
    return backup_path


def _merge_codex_mcp_config(existing: str, command: Path, args: list[str],
                            agent_key: str = "codex") -> str:
    snippet = _codex_mcp_snippet(command, args, agent_key).rstrip()
    section = re.compile(r"(?ms)^\[mcp_servers\.caddie\]\s*$.*?(?=^\[|\Z)")
    if section.search(existing):
        return section.sub(snippet + "\n", existing).rstrip() + "\n"
    return (existing.rstrip() + ("\n\n" if existing.strip() else "") + snippet + "\n")


def _merge_claude_mcp_config(existing: str, command: Path, args: list[str],
                             agent_key: str = "external_agent") -> str:
    try:
        payload = json.loads(existing or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(409, "MCP 客户端的配置文件不是有效 JSON；为保护原配置，Caddie 没有写入。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(409, "MCP 客户端的配置根节点必须是对象；Caddie 没有写入。")
    servers = payload.get("mcpServers") or {}
    if not isinstance(servers, dict):
        raise HTTPException(409, "MCP 客户端的 mcpServers 格式异常；Caddie 没有写入。")
    servers["caddie"] = {
        "command": str(command), "args": args,
        "env": {"CADDIE_AGENT_KEY": agent_key},
    }
    payload["mcpServers"] = servers
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _merge_json_mcp_config(existing: str, command: Path, args: list[str],
                           agent_key: str = "external_agent") -> str:
    """Merge Caddie into a standard MCP JSON file while preserving other servers."""
    return _merge_claude_mcp_config(existing, command, args, agent_key)


def _agent_connection_registry(home: Path | None = None) -> dict:
    path = (home or Path.home()) / ".caddie" / "agent-connections.json"
    try:
        value = json.loads(_read_local_text(path) or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _agent_connection_status(client_key: str, registry: dict) -> dict:
    item = registry.get(client_key) or {}
    return {
        "verified": bool(item.get("last_seen_at")),
        "last_seen_at": item.get("last_seen_at"),
        "protocol_version": item.get("protocol_version"),
        "tool_count": int(item.get("tool_count") or 0),
        "process_id": item.get("process_id"),
    }


def _agent_integrations_payload(home: Path | None = None) -> dict:
    paths = _agent_integration_paths(home)
    command, args, cli_script = paths["mcp_command"], paths["mcp_args"], paths["cli_script"]
    codex_config, claude_config, workbuddy_config = (
        paths["codex"], paths["claude_desktop"], paths["workbuddy"],
    )
    root = home or Path.home()
    if os.name == "nt":
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or (root / "AppData" / "Local"))
        codex_detected = (root / ".codex").exists()
        claude_detected = claude_config.parent.exists() or any(
            (local_appdata / name).exists() for name in ("Claude", "Programs/Claude")
        )
    else:
        codex_detected = any(path.exists() for path in (
            root / ".codex", Path("/Applications/Codex.app"), root / "Applications" / "Codex.app",
        ))
        claude_detected = any(path.exists() for path in (
            claude_config.parent, Path("/Applications/Claude.app"), root / "Applications" / "Claude.app",
        ))
    workbuddy_detected = any(path.exists() for path in (
        workbuddy_config.parent, Path("/Applications/WorkBuddy.app"),
        root / "Applications" / "WorkBuddy.app",
    ))
    connection_registry = _agent_connection_registry(home)
    if paths["packaged"]:
        cli_config_path = str(cli_script)
        cli_snippet = f'"{cli_script}" bootstrap --agent-key external_agent'
        cli_steps = ["复制命令", "在终端运行", "按输出的规则与审批范围执行"]
    else:
        cli_config_path = str(cli_script)
        cli_snippet = f'zsh "{cli_script}" bootstrap --agent-key external_agent'
        cli_steps = ["复制命令", "在项目目录或任意终端运行", "按输出的规则与审批范围执行"]
    return {
        "mcp_command": str(command), "mcp_args": args, "cli_script": str(cli_script),
        "packaged": paths["packaged"],
        "starter_prompt": (
            "请连接 Caddie，先调用 workspace_bootstrap，再用 get_workspace_map 定位当前岗位、"
            "项目和可写目录，只读取完成任务所需的最小上下文。需要写回时优先调用 "
            "save_career_document 并明确 destination；告诉我结果是草稿还是待确认候选，"
            "不要声称尚未确认的内容已经写入正式资料。"
        ),
        "clients": [
            {"key": "codex", "name": "Codex", "detected": codex_detected,
             "configured": _codex_is_configured(codex_config, command, args),
             **_agent_connection_status("codex", connection_registry),
             "config_path": str(codex_config), "snippet": _codex_mcp_snippet(command, args, "codex"),
            "primary_action": "一键接入", "can_install": True,
             "official_url": "https://openai.com/codex/",
             "pricing_url": "https://openai.com/chatgpt/pricing/",
             "pricing_summary": "包含于 ChatGPT Plus、Pro、Business、Enterprise 或 Edu；超额可购买 Credits",
             "recommendation": "适合复杂文件任务和受控迁移；优先使用工作区权限与审批模式。",
             "steps": ["点击「一键接入」", "Caddie 会备份并写入连接配置", "重启 Codex，开启一个新对话"],
             "restart_hint": "重启 Codex；新的对话就能调用 Caddie。"},
            {"key": "claude_desktop", "name": "Claude Desktop", "detected": claude_detected,
             "configured": _claude_is_configured(claude_config, command, args),
             **_agent_connection_status("claude_desktop", connection_registry),
             "config_path": str(claude_config),
             "snippet": _claude_mcp_snippet(command, args, "claude_desktop"),
             "primary_action": "一键接入", "can_install": True,
             "official_url": "https://claude.com/download",
             "pricing_url": "https://claude.com/pricing",
             "pricing_summary": "Free；Pro $20/月；Max 5x $100/月；Max 20x $200/月",
             "recommendation": "适合长文档问答和写作；中国大陆用户需先确认地区与网络可用性。",
             "steps": ["点击「一键接入」", "Caddie 会备份并合并 Claude 的 MCP 配置", "重启 Claude Desktop"],
             "restart_hint": "重启 Claude Desktop。"},
            {"key": "workbuddy", "name": "WorkBuddy", "detected": workbuddy_detected,
             "configured": _json_mcp_is_configured(workbuddy_config, command, args),
             **_agent_connection_status("workbuddy", connection_registry),
             "config_path": str(workbuddy_config),
             "snippet": _claude_mcp_snippet(command, args, "workbuddy"),
             "primary_action": "一键接入", "can_install": True,
             "official_url": "https://copilot.tencent.com/work/",
             "pricing_url": "https://cloud.tencent.cn/act/pro/workbuddy",
             "pricing_summary": "个人订阅以客户端为准；企业专享版官网活动价约 ¥316/用户/月",
             "recommendation": "适合腾讯文档、会议、邮箱等办公生态；复杂任务建议拆分并保留日志。",
             "steps": ["点击「一键接入」", "Caddie 会备份并合并 WorkBuddy 的用户级 MCP 配置",
                       "在 WorkBuddy 的插件 → MCP 服务器中确认 Caddie 为绿色"],
             "restart_hint": "重新加载 WorkBuddy；随后在 MCP 管理页确认 Caddie 已连接。"},
        ],
    }


@app.get("/api/agent/integrations")
def agent_integrations():
    """Local connection guide and status. No credentials leave this device."""
    return _agent_integrations_payload()


@app.post("/api/agent/integrations/{client_key}/install")
def install_agent_integration(client_key: str, body: AgentIntegrationInstallIn):
    if client_key not in {"codex", "claude_desktop", "workbuddy"}:
        raise HTTPException(400, "该接入方式不需要安装配置")
    if not body.confirmed:
        raise HTTPException(400, "请确认后再写入外部 Agent 配置")
    paths = _agent_integration_paths()
    command, args = paths["mcp_command"], paths["mcp_args"]
    config_path = paths[client_key]
    existing = _read_local_text(config_path)
    content = (_merge_codex_mcp_config(existing, command, args, client_key)
               if client_key == "codex"
               else _merge_json_mcp_config(existing, command, args, client_key))
    backup_path = _integration_backup_and_write(config_path, content)
    _commit(f"接入外部 Agent：{client_key}")
    clients = _agent_integrations_payload()["clients"]
    return {"ok": True, "client_key": client_key, "backup_path": backup_path,
            "integration": next(item for item in clients if item["key"] == client_key)}


def _migration_task_prompt(task_id: int, body: AgentMigrationStartIn) -> str:
    roots = "\n".join(f"- {path}" for path in body.roots if path.strip()) or "- 请先询问我选择或授权资料目录"
    include_types = "、".join(x.strip() for x in body.include_types if x.strip()) or "全部求职资料"
    notes = body.notes.strip() or "无"
    return f"""请使用 Caddie 完成一次受控的求职资料迁移。迁移任务 ID：{task_id}。

范围
- 资料目录：
{roots}
- 优先识别：{include_types}
- 额外说明：{notes}

执行协议
1. 先调用 workspace_bootstrap 和 get_workspace_map，理解 Caddie 已有结构与写入边界。
2. 第一阶段只盘点，不写入：扫描我授权的目录，按“个人资料、简历、经历、项目、岗位/JD、面试、其他”汇总数量；识别重复版本、明显冲突和可能包含隐私的文件。
3. 向我展示迁移方案：每类准备迁入什么、合并到哪里、哪些需要我确认。没有得到我同意前，不创建草稿或候选修改。
4. 得到同意后分批迁移，每批最多 10 项。正式事实、数字、贡献边界、岗位排期和文档覆盖必须提交候选，不能声称已经生效；通用产出可保存为带来源的草稿。
5. 每完成一批，调用 append_task_event 更新任务 {task_id}，写明已处理、待确认、跳过、冲突和下一批内容。
6. 结束时给出迁移报告：扫描数、迁入数、重复数、跳过数、冲突数、仍需人工处理项，并提醒我回到 Caddie 确认候选。

质量要求
- 保留来源文件路径和可追溯依据，不根据文件名猜测事实。
- 同一内容的多个版本先建立版本关系，不要重复生成经历或项目。
- 用户已有内容优先；发现冲突时并列展示，不自行覆盖。
- 只读取完成迁移所需的文件，不读取无关私人目录。"""


@app.get("/api/agent/migrations")
def list_agent_migrations(limit: int = 20):
    items = [
        _agent_task_payload(item)
        for item in db.list_agent_tasks(limit=200)
        if item.get("task_type") == "data_migration"
    ]
    return {"items": items[:min(max(limit, 1), 100)]}


@app.post("/api/agent/migrations")
def start_agent_migration(body: AgentMigrationStartIn):
    valid_agents = {"codex", "claude_desktop", "workbuddy", "external_agent", "cli"}
    agent_key = body.agent_key.strip() or "external_agent"
    if agent_key not in valid_agents:
        raise HTTPException(400, "不支持的 Agent 类型")
    roots = []
    for value in body.roots:
        text = value.strip()
        if not text:
            continue
        path = Path(text).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise HTTPException(400, f"资料目录不存在：{text}")
        roots.append(str(path))
    task_id = db.create_agent_task({
        "task_type": "data_migration",
        "title": "从已有资料迁移到 Caddie",
        "instruction": "由外部 Agent 盘点、规划并分批提交求职资料，所有正式写入均需确认。",
        "object_type": "workspace",
        "status": "active",
        "priority": "normal",
        "assigned_expert": "experience_detective",
        "created_by": "user",
        "execution_owner": "external",
        "actor_type": "user",
        "actor_key": agent_key,
        "context_json": json.dumps({
            "migration": True,
            "agent_key": agent_key,
            "roots": roots,
            "include_types": body.include_types,
            "notes": body.notes.strip(),
            "phase": "inventory",
        }, ensure_ascii=False),
    })
    prompt = _migration_task_prompt(task_id, AgentMigrationStartIn(
        agent_key=agent_key, roots=roots, include_types=body.include_types, notes=body.notes,
    ))
    db.update_agent_task(task_id, instruction=prompt)
    db.create_agent_event({
        "task_id": task_id, "event_type": "migration_created",
        "label": "等待 Agent 开始盘点",
        "detail": "尚未写入任何职业资料；先复制任务说明到已接入的 Agent。",
        "status": "pending",
        "payload_json": json.dumps({"phase": "inventory", "roots": roots}, ensure_ascii=False),
    })
    _commit("创建外部 Agent 资料迁移任务")
    return {"task_id": task_id, "status": "active", "phase": "inventory",
            "prompt": prompt, "requires_confirmation": True}


@app.post("/api/agent/context-package")
def get_agent_context_package(body: AgentContextPackageIn):
    """Return selected, versioned context for an external Agent.

    Unlike a database dump, the package makes explicit which records were read
    and which revision must still match before the Agent proposes an overwrite.
    """
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位求职线不存在")
    if body.project_id and not db.get_project(body.project_id):
        raise HTTPException(404, "项目不存在")
    if body.interview_round_id and not interview_store.get_round(body.interview_round_id):
        raise HTTPException(404, "面试轮次不存在")
    try:
        return caddie_context.build_agent_context_package(
            track_id=body.track_id, project_id=body.project_id,
            interview_round_id=body.interview_round_id,
            document_type=body.document_type, document_id=body.document_id,
            intent=body.intent.strip() or "external_agent", query=(body.query or "").strip() or None,
            detail=body.detail,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/agent/changes")
def get_agent_changes(after: int = 0, scope_type: Optional[str] = None,
                      scope_id: Optional[int] = None, limit: int = 100):
    items = db.list_workspace_change_events(after, scope_type, scope_id, limit)
    return {"items": items, "next_cursor": items[-1]["id"] if items else max(0, after),
            "latest_cursor": db.latest_workspace_change_cursor()}


@app.post("/api/agent/external-runs")
def start_external_agent_run(body: ExternalAgentRunIn):
    if not body.title.strip() or not body.instruction.strip():
        raise HTTPException(400, "运行标题和任务说明不能为空")
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位求职线不存在")
    route = agent_runtime.coordinate(body.instruction, body.object_type, body.object_id, body.track_id)
    task_id = db.create_agent_task({
        "task_type": route["task_type"], "title": body.title.strip(), "instruction": body.instruction.strip(),
        "object_type": body.object_type, "object_id": body.object_id, "track_id": body.track_id,
        "status": "active", "assigned_expert": route["expert_key"], "created_by": "external_agent",
        "execution_owner": "external", "actor_type": "external_agent", "actor_key": body.agent_key,
        "context_json": json.dumps({"routing_reason": route["reason"], "external_context": body.context or {}}, ensure_ascii=False),
    })
    run_id = db.create_agent_run({
        "task_id": task_id, "run_type": "external", "expert_key": body.agent_key,
        "model_profile": "external", "status": "running", "actor_type": "external_agent", "actor_key": body.agent_key,
        "input_json": json.dumps({"instruction": body.instruction, "context": body.context or {}}, ensure_ascii=False),
    })
    db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "external_started",
                           "label": "外部 Agent 开始工作", "detail": f"来源：{body.agent_key}", "status": "running"})
    db.create_workspace_change_event({"event_type": "external_run_started", "target_type": "agent_task", "target_id": task_id,
                                      "scope_type": "track" if body.track_id else "global", "scope_id": body.track_id,
                                      "actor_type": "external_agent", "actor_key": body.agent_key,
                                      "summary": f"外部 Agent 开始：{body.title.strip()}"})
    _commit(f"外部 Agent 开始任务：{body.title.strip()[:60]}")
    return {"task": _agent_task_payload(db.get_agent_task(task_id)), "run_id": run_id,
            "requires_confirmation_for_facts": True}


@app.post("/api/agent/external-runs/{task_id}/events")
def append_external_agent_event(task_id: int, body: ExternalAgentEventIn):
    run_id = _external_run_for_task(task_id)
    if body.status not in {"pending", "running", "done", "failed"}:
        raise HTTPException(400, "status 必须是 pending、running、done 或 failed")
    event_id = db.create_agent_event({"task_id": task_id, "run_id": run_id, "event_type": "external_progress",
                                      "label": body.label.strip() or "外部 Agent 进度", "detail": body.detail.strip(),
                                      "status": body.status, "payload_json": json.dumps(body.payload or {}, ensure_ascii=False)})
    _commit("外部 Agent 更新任务进度")
    return {"event_id": event_id, "task_id": task_id, "run_id": run_id}


@app.post("/api/agent/external-runs/{task_id}/complete")
def complete_external_agent_run(task_id: int, summary: str = ""):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    for run in task.get("runs") or []:
        if run.get("run_type") == "external" and run.get("status") in {"queued", "running", "ready"}:
            db.update_agent_run(run["id"], status="completed", summary=summary.strip() or run.get("summary"))
    pending = [x for x in (db.get_agent_task(task_id).get("changes") or []) if x.get("status") == "pending"]
    db.update_agent_task(task_id, status="review" if pending else "completed", result_summary=summary.strip() or None)
    db.create_agent_event({"task_id": task_id, "event_type": "external_completed", "label": "外部 Agent 已完成",
                           "detail": "有候选修改等待确认" if pending else "未提交需要确认的修改", "status": "done"})
    _commit("外部 Agent 完成任务")
    return _agent_task_payload(db.get_agent_task(task_id))


@app.post("/api/agent/draft-assets")
def create_external_draft_asset(body: ExternalDraftAssetIn):
    if not body.title.strip() or not body.body.strip():
        raise HTTPException(400, "草稿标题和正文不能为空")
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "岗位求职线不存在")
    if body.project_id and not db.get_project(body.project_id):
        raise HTTPException(404, "项目不存在")
    if body.task_id:
        _external_run_for_task(body.task_id, body.agent_key)
    aid = db.create_asset({
        "asset_type": body.asset_type, "track_id": body.track_id, "project_id": body.project_id,
        "title": body.title.strip(), "body": body.body, "status": "draft",
        "provenance_json": json.dumps({"source": "external_agent", "agent_key": body.agent_key,
                                         "task_id": body.task_id, **(body.provenance or {})}, ensure_ascii=False),
    })
    db.create_workspace_change_event({"event_type": "draft_asset_created", "target_type": "asset", "target_id": aid,
                                      "scope_type": "track" if body.track_id else ("project" if body.project_id else "global"),
                                      "scope_id": body.track_id or body.project_id, "actor_type": "external_agent",
                                      "actor_key": body.agent_key, "summary": f"新增草稿资产：{body.title.strip()[:60]}"})
    _commit(f"外部 Agent 保存草稿资产：{body.title.strip()[:60]}")
    return {"asset": db.get_asset(aid), "status": "draft"}


@app.post("/api/agent/proposals/fact")
def propose_external_fact(body: ExternalFactProposalIn):
    run_id = _external_run_for_task(body.task_id, body.agent_key)
    if not body.predicate.strip() or not body.value_text.strip():
        raise HTTPException(400, "事实谓词和内容不能为空")
    metadata = {"subject_type": body.subject_type, "subject_id": body.subject_id,
                "scope_type": body.scope_type, "scope_id": body.scope_id,
                "predicate": body.predicate.strip(), "confidence": body.confidence,
                "evidence": body.evidence, "agent_key": body.agent_key}
    change_id = db.create_proposed_change({
        "task_id": body.task_id, "run_id": run_id, "action_type": "create_fact", "target_type": "career_fact",
        "parent_type": body.subject_type, "parent_id": body.subject_id, "proposed_title": body.predicate.strip(),
        "proposed_content": body.value_text.strip(), "reason": body.reason.strip() or "外部 Agent 提交事实候选",
        "scope_type": body.scope_type, "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(body.task_id, status="review")
    db.create_agent_event({"task_id": body.task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交事实候选", "detail": "尚未写入事实库，等待用户确认", "status": "done"})
    _commit("外部 Agent 提交事实候选")
    return {"change_id": change_id, "requires_confirmation": True}


@app.post("/api/agent/proposals/feedback")
def propose_external_feedback(body: ExternalFeedbackProposalIn):
    run_id = _external_run_for_task(body.task_id, body.agent_key)
    if not body.original_text.strip():
        raise HTTPException(400, "反馈内容不能为空")
    metadata = {"scope": body.scope, "scope_id": body.scope_id, "category": body.category,
                "polarity": body.polarity, "strength": body.strength, "directive": body.directive,
                "agent_key": body.agent_key}
    change_id = db.create_proposed_change({
        "task_id": body.task_id, "run_id": run_id, "action_type": "create_feedback", "target_type": "feedback_note",
        "parent_type": body.scope, "parent_id": body.scope_id, "proposed_title": "反馈约束",
        "proposed_content": body.original_text.strip(), "reason": body.reason.strip() or "外部 Agent 提交反馈候选",
        "scope_type": body.scope, "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(body.task_id, status="review")
    db.create_agent_event({"task_id": body.task_id, "run_id": run_id, "event_type": "review",
                           "label": "提交反馈候选", "detail": "尚未注入后续生成约束，等待用户确认", "status": "done"})
    _commit("外部 Agent 提交反馈候选")
    return {"change_id": change_id, "requires_confirmation": True}


@app.post("/api/agent/proposals/track-knowledge")
def propose_external_track_knowledge(body: ExternalTrackKnowledgeProposalIn):
    """Queue a document for a named folder in the real job preparation workspace."""
    if not body.title.strip() or not body.body.strip():
        raise HTTPException(400, "文档标题和正文不能为空")
    if body.mastery not in {"new", "learning", "familiar", "mastered"}:
        raise HTTPException(400, "mastery 必须是 new、learning、familiar 或 mastered")
    track = db.get_job_track(body.track_id)
    if not track:
        raise HTTPException(404, "岗位求职线不存在")
    folder_names = {key: name for key, name, _order in db.DEFAULT_TRACK_KNOWLEDGE_FOLDERS}
    if body.folder_key not in folder_names:
        raise HTTPException(400, f"folder_key 必须是：{'、'.join(folder_names)}")
    run_id = _external_run_for_task(body.task_id, body.agent_key)
    folders, _created = db.ensure_track_knowledge_folders(body.track_id)
    folder = next((item for item in folders if item.get("name") == folder_names[body.folder_key]), None)
    if not folder:
        raise HTTPException(500, "岗位准备目录初始化失败")
    metadata = {
        "track_id": body.track_id, "folder_id": folder["id"], "folder_key": body.folder_key,
        "folder_name": folder["name"], "topic": body.topic.strip(), "mastery": body.mastery,
        "evidence": body.evidence, "agent_key": body.agent_key, "source": "external_agent",
    }
    change_id = db.create_proposed_change({
        "task_id": body.task_id, "run_id": run_id, "action_type": "create_track_knowledge",
        "target_type": "knowledge_item", "parent_type": "job_track", "parent_id": body.track_id,
        "proposed_title": body.title.strip(), "proposed_content": body.body.strip(),
        "reason": body.reason.strip() or f"提议保存到当前岗位的「{folder['name']}」",
        "scope_type": "track", "metadata_json": json.dumps(metadata, ensure_ascii=False),
    })
    db.update_agent_task(body.task_id, status="review", result_summary="外部 Agent 已提交岗位准备文档，等待确认")
    db.create_agent_event({
        "task_id": body.task_id, "run_id": run_id, "event_type": "review",
        "label": "提交岗位准备文档", "detail": f"尚未写入「{folder['name']}」，等待用户确认",
        "status": "done", "payload_json": json.dumps({"change_id": change_id, "folder_key": body.folder_key}, ensure_ascii=False),
    })
    _commit(f"外部 Agent 提交岗位准备文档：{body.title.strip()[:60]}")
    return {"change_id": change_id, "requires_confirmation": True,
            "destination": {"track_id": body.track_id, "folder_key": body.folder_key, "folder_name": folder["name"]}}


@app.post("/api/agent/tasks/{task_id}/cancel")
def cancel_agent_task(task_id: int):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    if task.get("status") in {"completed", "failed", "cancelled"}:
        return _agent_task_payload(task)
    for run in task.get("runs") or []:
        if run.get("status") not in {"completed", "failed", "cancelled"}:
            db.update_agent_run(run["id"], status="cancelled")
    for change in task.get("changes") or []:
        if change.get("status") == "pending":
            db.update_proposed_change(change["id"], "ignored")
    db.update_agent_task(task_id, status="cancelled")
    db.create_agent_event({
        "task_id": task_id, "event_type": "cancelled", "label": "任务已取消",
        "detail": "未确认的候选修改不会写入", "status": "done",
    })
    return _agent_task_payload(db.get_agent_task(task_id))


@app.get("/api/agent/runs/{run_id}")
def get_agent_run(run_id: int):
    conn = db.get_db()
    run = db.one(conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())
    if run:
        run["events"] = db.rows(conn.execute(
            "SELECT * FROM agent_events WHERE run_id=? ORDER BY sequence,id", (run_id,)).fetchall())
        run["changes"] = db.rows(conn.execute(
            "SELECT * FROM proposed_changes WHERE run_id=? ORDER BY id", (run_id,)).fetchall())
    conn.close()
    if not run:
        raise HTTPException(404, "Agent 运行不存在")
    return run


EXPERIENCE_DETECTIVE_PROMPT = """你是 Caddie 的经历侦探。请审计一个项目经历，但不要替用户编造或直接重写项目文档。
目标是区分：已经确认的事实、表述中存在的模糊点、缺少证据的关键结论、值得继续追问的问题。

要求：
1. 追问必须具体且能推动用户补充真实信息，不能问泛泛的“还有吗”。
2. 优先覆盖本人贡献边界、关键决策、方法细节、数字口径、结果归因、协作与反事实。
3. 已经在现有题库中出现的问题不要重复创建；如果对话里已经形成了该问题的回答，
   可以返回同一问题和 answer，系统会把回答作为待确认更新。
4. 不得推测用户“主导”“负责”或编造任何数字。
5. 最多提出 8 条追问；kind 只能是 factual 或 reflective；category 使用稳定短标签。
6. answer 只能整理最近项目对话里已经出现的回答，不得替用户补写新事实；没有可靠回答就留空。

只返回严格 JSON：
{"summary":"审计结论","confirmed_facts":["已确认事实"],"uncertainties":["模糊或缺证据之处"],"followups":[
  {"question":"具体追问","answer":"对话中已有的建议回答或用户回答，没有则为空","kind":"factual","category":"项目贡献","reason":"为什么必须补充","severity":"blocker"}
]}
"""


def _requests_project_document_update(instruction: str) -> bool:
    text = re.sub(r"\s+", "", instruction or "")
    return any(marker in text for marker in (
        "修改项目文档", "更新项目文档", "整理进项目", "补充进项目",
        "写进项目", "整理项目文档", "重写项目", "完善项目文档",
        "合并到项目文档", "合并进项目文档", "放进项目文档",
    ))


def _abort_if_agent_task_cancelled(task_id: int) -> None:
    current = db.get_agent_task(task_id)
    if current and current.get("status") == "cancelled":
        raise RuntimeError("Agent 任务已取消")


def _run_experience_detective_task(task: dict) -> dict:
    _guard_agent_task(task, "experience_detective")
    project_id = task.get("object_id") if task.get("object_type") == "project" else None
    project = db.get_project(project_id) if project_id else None
    if not project:
        raise ValueError("经历侦探需要绑定一个项目")
    _refresh_project_local_sources(project_id)
    expert = db.get_agent_expert("experience_detective") or {}
    model_selection = _expert_model_selection(expert)
    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "primary",
        "expert_key": "experience_detective",
        **_agent_run_model_fields(model_selection),
        "status": "running",
        "input_json": json.dumps({"project_id": project_id}, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    db.update_agent_task(task["id"], status="active")
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "context",
        "label": "读取项目事实", "detail": "正在读取项目文档、经历归属和既有追问题库",
    })
    existing = db.list_followups(project_id=project_id)
    discussion = db.get_chat_history(f"proj-{project_id}", limit=40)
    fact_documents = interview_store.list_documents(
        document_type="project_fact", project_id=project_id
    )
    context_data = caddie_context.build_context(
        project_id=project_id, track_id=task.get("track_id"),
        intent="experience_audit", query=task.get("instruction") or "",
    )
    existing_text = "\n".join(f"- {x.get('question')}" for x in existing[:40]) or "（无）"
    discussion_text = "\n".join(
        f"{'用户' if item.get('role') == 'user' else 'Caddie'}：{item.get('content') or ''}"
        for item in discussion
    ) or "（无）"
    material = (
        f"【任务】{task.get('instruction')}\n"
        f"【项目】{project.get('name')}\n"
        f"【一句话】{project.get('one_liner') or '（无）'}\n"
        f"【项目文档】\n{(project.get('document') or '')[:18000]}\n\n"
        f"【补充事实文档】\n"
        + "\n\n".join(
            f"### {item.get('title')}\n{(item.get('body') or '')[:6000]}"
            for item in fact_documents[:8]
        )
        + "\n\n"
        f"【本项目最近对话】\n{discussion_text[-16000:]}\n\n"
        f"【既有追问，禁止重复】\n{existing_text}\n\n"
        f"【关联上下文】\n{context_data.get('text','')[:10000]}"
    )
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "audit",
        "label": "审计事实与证据", "detail": f"已读取 {len(existing)} 条既有追问，开始检查贡献边界和证据口径",
        "status": "running",
    })
    raw = ai.chat([{"role": "user", "content": material}],
                  system=EXPERIENCE_DETECTIVE_PROMPT, max_tokens=2200,
                  provider=model_selection["provider"])
    _record_agent_actual_model(task["id"], run_id)
    _abort_if_agent_task_cancelled(task["id"])
    try:
        data = ai.extract_json(raw)
    except Exception:
        data = None
    if not isinstance(data, dict):
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "harness_repair",
            "label": "修复结构化输出",
            "detail": "首轮返回不是有效 JSON，正在保留原始内容并要求模型仅修复格式",
            "status": "running",
        })
        repaired = ai.chat(
            [{"role": "user", "content": (
                "请把下面的审计结果修复成系统要求的严格 JSON。"
                "不得增加原文没有的事实或数字；只修复结构和字段。\n\n" + raw
            )}],
            system=EXPERIENCE_DETECTIVE_PROMPT, max_tokens=2400,
            provider=model_selection["provider"],
        )
        _record_agent_actual_model(task["id"], run_id)
        _abort_if_agent_task_cancelled(task["id"])
        try:
            data = ai.extract_json(repaired)
        except Exception:
            data = None
    if not isinstance(data, dict):
        raise ValueError("经历侦探连续两次未返回有效结构，请重试或切换结构化输出更稳定的模型")
    existing_by_norm = {
        re.sub(r"\s+", "", x.get("question") or "").lower(): x
        for x in existing if (x.get("question") or "").strip()
    }
    existing_norm = set(existing_by_norm)
    created = 0
    updated = 0
    for item in (data.get("followups") or [])[:8]:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        norm = re.sub(r"\s+", "", question).lower()
        if not question:
            continue
        kind = item.get("kind") if item.get("kind") in {"factual", "reflective"} else "factual"
        metadata = {
            "kind": kind, "category": str(item.get("category") or "其他")[:24],
            "severity": item.get("severity") or "fixable",
            "origin": "experience_detective", "reason": item.get("reason") or "",
        }
        current = existing_by_norm.get(norm)
        if current:
            if answer and answer != (current.get("answer") or "").strip():
                metadata["base_answer_hash"] = hashlib.sha256(
                    (current.get("answer") or "").encode("utf-8")
                ).hexdigest()
                db.create_proposed_change({
                    "task_id": task["id"], "run_id": run_id,
                    "action_type": "update_followup", "target_type": "followup",
                    "target_id": current["id"], "parent_type": "project", "parent_id": project_id,
                    "proposed_title": question, "proposed_content": answer,
                    "reason": item.get("reason") or "把本轮对话中的回答合并到既有追问",
                    "status": "pending", "metadata_json": json.dumps(metadata, ensure_ascii=False),
                })
                updated += 1
            continue
        if norm in existing_norm:
            continue
        db.create_proposed_change({
            "task_id": task["id"], "run_id": run_id,
            "action_type": "create_followup", "target_type": "followup",
            "parent_type": "project", "parent_id": project_id,
            "proposed_title": question, "proposed_content": answer,
            "reason": item.get("reason") or "补齐项目事实与证据",
            "status": "pending", "metadata_json": json.dumps(metadata, ensure_ascii=False),
        })
        existing_norm.add(norm); created += 1
    document_changes = 0
    if _requests_project_document_update(task.get("instruction") or ""):
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "editing",
            "label": "形成项目文档候选",
            "detail": "保留已确认事实，以完整 Markdown 候选替代直接覆盖",
            "status": "running",
        })
        document_system = """你是 Caddie 的项目文档编辑。请根据现有项目文档、关联上下文和审计结果，形成一份完整可替换的项目文档。
保留原文中有价值的事实和细节；按背景、问题定义、我的动作与关键决策、协作与推进、结果与价值、待确认信息组织。
不得编造数字、职责或结果；不确定内容明确标为待确认。追问不得写进正文题库。
严格使用以下分隔符，不要 JSON，不要代码围栏：
SUMMARY: 一句话说明本次修改
TITLE: 项目标题
===DOCUMENT===
完整 Markdown 正文"""
        audit_brief = json.dumps({
            "confirmed_facts": data.get("confirmed_facts") or [],
            "uncertainties": data.get("uncertainties") or [],
        }, ensure_ascii=False)
        document_raw = ai.chat(
            [{"role": "user", "content": material + "\n\n【审计结果】\n" + audit_brief}],
            system=document_system, max_tokens=4600,
            provider=model_selection["provider"],
        )
        _record_agent_actual_model(task["id"], run_id)
        _abort_if_agent_task_cancelled(task["id"])
        doc_summary, doc_title, document = _parse_agent_document(
            document_raw, project.get("name") or "未命名项目"
        )
        if not document.strip():
            raise ValueError("项目文档候选正文为空")
        diff = "\n".join(difflib.unified_diff(
            (project.get("document") or "").splitlines(), document.splitlines(),
            fromfile="当前项目文档", tofile="候选项目文档", lineterm="",
        ))
        db.create_proposed_change({
            "task_id": task["id"], "run_id": run_id,
            "action_type": "update_project_document", "target_type": "project",
            "target_id": project_id, "proposed_title": doc_title,
            "proposed_content": document, "diff": diff[:32000],
            "reason": doc_summary, "scope_type": "project", "status": "pending",
            "metadata_json": json.dumps({
                "base_hash": hashlib.sha256(
                    (project.get("document") or "").encode("utf-8")
                ).hexdigest(),
                "source_expert": "experience_detective",
            }, ensure_ascii=False),
        })
        document_changes = 1
    output = {
        "summary": data.get("summary") or f"形成 {created} 条待确认追问",
        "confirmed_facts": (data.get("confirmed_facts") or [])[:12],
        "uncertainties": (data.get("uncertainties") or [])[:12],
        "proposed_followups": created,
        "proposed_followup_updates": updated,
        "proposed_document_updates": document_changes,
    }
    summary = output["summary"]
    db.update_agent_run(run_id, status="ready", output_json=json.dumps(output, ensure_ascii=False), summary=summary)
    db.update_agent_task(task["id"], status="review", result_summary=summary)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "review",
        "label": "完成经历审计",
        "detail": f"形成 {created} 条候选追问、{updated} 条追问回答更新、{document_changes} 份项目文档候选，等待用户确认",
    })
    return db.get_agent_task(task["id"])


def _run_standalone_knowledge_task(task: dict) -> dict:
    """Prepare a reviewable whole-document update without requiring a job track."""
    knowledge_id = task.get("object_id") if task.get("object_type") == "knowledge_item" else None
    item = db.get_knowledge_item(knowledge_id) if knowledge_id else None
    if not item:
        raise ValueError("知识教练需要绑定一篇有效的知识文档")
    task_context = _json_loads_safe(task.get("context_json"), {}) or {}
    base_candidate = task_context.get("base_candidate") or {}
    working_title = (base_candidate.get("title") or item.get("title") or "").strip()
    working_content = base_candidate.get("content") or item.get("content") or ""

    expert = db.get_agent_expert("knowledge_coach") or {}
    model_selection = _expert_model_selection(expert)
    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "primary",
        "expert_key": "knowledge_coach",
        **_agent_run_model_fields(model_selection),
        "status": "running",
        "input_json": json.dumps({
            "knowledge_item_id": knowledge_id,
            "instruction": task.get("instruction") or "",
        }, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    db.update_agent_task(task["id"], status="active")
    scope = _enrich_knowledge_item(item).get("scope_label") or item.get("scope_type") or "个人通用"
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "context",
        "label": "读取知识文档",
        "detail": f"已读取《{item.get('title') or '未命名知识'}》及其复用范围：{scope}",
        "status": "done",
    })
    material = (
        f"【任务】\n{task.get('instruction') or ''}\n\n"
        f"【当前标题】\n{working_title}\n\n"
        f"【复用范围】\n{scope}\n\n"
        f"【主题】\n{item.get('topic') or '（未填写）'}\n\n"
        f"【当前文档】\n{working_content}"
    )
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "editing",
        "label": "整理候选文档", "detail": "正在保留有效原文并按任务形成完整候选版本",
        "status": "running",
    })
    raw = ai.chat(
        [{"role": "user", "content": material}],
        system=KNOWLEDGE_SPACE_EDITOR_PROMPT, max_tokens=4000,
        provider=model_selection["provider"],
    )
    _record_agent_actual_model(task["id"], run_id)
    _abort_if_agent_task_cancelled(task["id"])
    summary, title, document = _parse_agent_document(raw, working_title or "未命名知识")
    diff = "\n".join(difflib.unified_diff(
        working_content.splitlines(), document.splitlines(),
        fromfile="上一版候选" if base_candidate else "当前文档",
        tofile="候选修改", lineterm="",
    ))
    db.create_proposed_change({
        "task_id": task["id"], "run_id": run_id,
        "action_type": "update_knowledge", "target_type": "knowledge_item",
        "target_id": knowledge_id, "proposed_title": title,
        "proposed_content": document, "diff": diff[:32000],
        "reason": summary, "scope_type": item.get("scope_type") or "global",
        "status": "pending",
        "metadata_json": json.dumps({
            "original_title": item.get("title") or "",
            "scope_type": item.get("scope_type") or "global",
            "base_hash": hashlib.sha256((item.get("content") or "").encode("utf-8")).hexdigest(),
        }, ensure_ascii=False),
    })
    output = {
        "summary": summary,
        "knowledge_item_id": knowledge_id,
        "proposed_title": title,
        "changed": title != (item.get("title") or "") or document != (item.get("content") or ""),
    }
    db.update_agent_run(
        run_id, status="ready", output_json=json.dumps(output, ensure_ascii=False),
        summary=summary,
    )
    db.update_agent_task(task["id"], status="review", result_summary=summary)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "review",
        "label": "候选文档已就绪", "detail": "完整候选版本尚未写入，等待用户确认",
        "status": "done",
    })
    return db.get_agent_task(task["id"])


def _run_global_knowledge_task(task: dict) -> dict:
    """Create a reviewable reusable knowledge document without a job track."""
    _guard_agent_task(task, "knowledge_coach")
    expert = db.get_agent_expert("knowledge_coach") or {}
    model_selection = _expert_model_selection(expert)
    project_id = task.get("object_id") if task.get("object_type") == "project" else None
    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "primary",
        "expert_key": "knowledge_coach",
        **_agent_run_model_fields(model_selection),
        "status": "running",
        "input_json": json.dumps({
            "project_id": project_id,
            "instruction": task.get("instruction") or "",
        }, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    db.update_agent_task(task["id"], status="active")
    assembled = caddie_context.build_context(
        project_id=project_id, intent="knowledge_coach",
        query=task.get("instruction") or "",
    )
    packed_context = harness.pack_context(assembled.get("text") or "", task, budget=28000)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "context",
        "label": "装配知识上下文",
        "detail": f"已读取个人资料与相关项目，使用 {packed_context['used']} 字",
        "status": "done",
        "payload_json": json.dumps(packed_context, ensure_ascii=False),
    })
    system = """你是 Caddie 的知识教练。请基于用户任务和已确认资料，形成一篇可长期复用的知识文档。
正文应讲清概念、原理、应用方式、与用户经历的连接及待补充项。区分已确认事实与一般知识，不得虚构用户经历。
严格使用下列分隔符返回，不要 JSON，不要代码围栏：
SUMMARY: 一句话结论
TITLE: 文档标题
===DOCUMENT===
完整 Markdown 正文"""
    material = (
        f"【用户任务】\n{task.get('instruction') or ''}\n\n"
        f"【用户经历概览】{_experience_context()}\n\n"
        f"【Caddie 已确认上下文】\n{packed_context['text']}"
    )
    raw = ai.chat(
        [{"role": "user", "content": material}], system=system,
        max_tokens=4200, provider=model_selection["provider"],
    )
    _record_agent_actual_model(task["id"], run_id)
    _abort_if_agent_task_cancelled(task["id"])
    summary, title, document = _parse_agent_document(raw, "未命名知识")
    report = harness.audit_document(title, document, "knowledge_coach")
    _record_harness_report(task["id"], run_id, report)
    if not document.strip():
        raise ValueError("知识教练没有生成可保存的正文")
    db.create_proposed_change({
        "task_id": task["id"], "run_id": run_id,
        "action_type": "create_global_knowledge",
        "target_type": "knowledge_item",
        "proposed_title": title, "proposed_content": document,
        "reason": summary, "scope_type": "global", "status": "pending",
        "metadata_json": json.dumps({
            "topic": title, "source_expert": "knowledge_coach",
        }, ensure_ascii=False),
    })
    output = {"summary": summary, "title": title, "scope_type": "global"}
    db.update_agent_run(
        run_id, status="ready", output_json=json.dumps(output, ensure_ascii=False),
        summary=summary,
    )
    db.update_agent_task(task["id"], status="review", result_summary=summary)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "review",
        "label": "知识候选已就绪", "detail": "等待确认后写入知识空间",
        "status": "done",
    })
    return db.get_agent_task(task["id"])


def _run_track_single_knowledge_task(task: dict) -> dict:
    """Generate exactly one durable candidate for the unified knowledge workspace."""
    track_id = task.get("track_id")
    track = db.get_job_track(track_id)
    if not track:
        raise ValueError("知识任务对应的岗位已经不存在")
    folders, _created = db.ensure_track_knowledge_folders(track_id)
    task_context = _json_loads_safe(task.get("context_json"), {}) or {}
    preferred = task_context.get("folder_name") or "专业知识"
    folder = next((item for item in folders if item.get("name") == preferred), None) or folders[0]
    expert = db.get_agent_expert("knowledge_coach") or {}
    model_selection = _expert_model_selection(expert)
    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "primary", "expert_key": "knowledge_coach",
        **_agent_run_model_fields(model_selection), "status": "running",
        "input_json": json.dumps(task_context, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    db.update_agent_task(task["id"], status="active")
    assembled = caddie_context.build_context(
        track_id=track_id, intent="knowledge_coach", query=task.get("instruction") or "",
    )
    packed = harness.pack_context(assembled.get("text") or "", task, budget=28000)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "context",
        "label": "知识教练读取岗位资料",
        "detail": f"已读取 JD、差距、经历、简历和已有知识；目标目录：{folder.get('name')}",
        "status": "done",
    })
    material = (
        f"【本轮目标】{task_context.get('goal') or '创建一篇岗位知识文档'}\n"
        f"【主题或差距】{task_context.get('topic') or '由用户指令确定'}\n"
        f"【用户指令】{task.get('instruction') or ''}\n\n"
        f"【当前岗位】{track.get('company') or ''} · {track.get('role') or track.get('target') or ''}\n\n"
        f"【已确认资料】\n{packed['text']}"
    )
    raw = ai.chat(
        [{"role": "user", "content": material}],
        system="""你是 Caddie 知识教练。一次只生成一篇完整的岗位知识文档；若用户要求多篇，
先在这一篇中给出规划，不要批量输出多篇。不得虚构经历。
严格返回：
SUMMARY: 一句话说明
TITLE: 标题
===DOCUMENT===
完整 Markdown 正文""",
        max_tokens=4200, provider=model_selection["provider"],
    )
    _record_agent_actual_model(task["id"], run_id)
    summary, title, document = _parse_agent_document(raw, task_context.get("topic") or "未命名知识")
    db.create_proposed_change({
        "task_id": task["id"], "run_id": run_id, "action_type": "create_track_knowledge",
        "target_type": "knowledge_item", "parent_type": "job_track", "parent_id": track_id,
        "proposed_title": title, "proposed_content": document, "reason": summary,
        "scope_type": "track", "status": "pending",
        "metadata_json": json.dumps({
            "track_id": track_id, "folder_id": folder["id"], "folder_name": folder.get("name"),
            "folder_key": folder.get("folder_key"), "topic": task_context.get("topic") or title,
            "source_expert": "knowledge_coach", "gap_id": task_context.get("gap_id"),
        }, ensure_ascii=False),
    })
    db.update_agent_run(run_id, status="ready", summary=summary)
    db.update_agent_task(task["id"], status="review", result_summary=summary)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "review",
        "label": "单篇知识候选已就绪", "detail": "等待确认后写入岗位准备知识",
        "status": "done",
    })
    return db.get_agent_task(task["id"])


def _generate_track_knowledge_plan_item(task, plan_item, track, folder, packed, model_selection,
                                        *, existing_change_id=None):
    title = (plan_item.get("title") or "未命名知识").strip()[:120]
    goal = (plan_item.get("goal") or f"完整讲清 {title}").strip()
    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "knowledge_document", "expert_key": "knowledge_coach",
        **_agent_run_model_fields(model_selection), "status": "running",
        "input_json": json.dumps({"title": title, "goal": goal}, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    metadata = {
        "track_id": track["id"], "folder_id": folder["id"], "folder_name": folder.get("name"),
        "folder_key": folder.get("folder_key"), "topic": title, "source_expert": "knowledge_coach",
        "plan_title": title, "plan_goal": goal,
        "source_candidate_ids": plan_item.get("source_candidate_ids") or [],
    }
    sources = [db.get_proposed_change(int(cid)) for cid in (plan_item.get("source_candidate_ids") or [])]
    sources = [item for item in sources if item]
    target_document = db.get_knowledge_item(plan_item.get("target_knowledge_item_id")) if plan_item.get("target_knowledge_item_id") else None
    try:
        if existing_change_id:
            db.update_proposed_change(existing_change_id, "generating")
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "editing",
            "label": f"生成《{title}》", "detail": goal, "status": "running",
        })
        source_text = "\n\n".join(
            f"《{item.get('proposed_title') or '未命名'}》\n{item.get('proposed_content') or ''}" for item in sources
        )
        target_text = (
            f"【绑定的正式文档】\n《{target_document.get('title')}》\n{target_document.get('content') or ''}\n\n"
            if target_document else ""
        )
        sibling_candidates = [
            item for item in (db.get_agent_task(task["id"]).get("changes") or [])
            if item.get("id") != existing_change_id and item.get("status") in {"pending", "applied"}
            and (item.get("proposed_content") or "").strip()
        ]
        quality_reference = max(sibling_candidates, key=lambda item: len(item.get("proposed_content") or ""), default=None)
        quality_text = ""
        if quality_reference:
            quality_text = (
                "【同组已完成文档的质量基准】\n"
                f"《{quality_reference.get('proposed_title') or '已完成文档'}》\n"
                f"{(quality_reference.get('proposed_content') or '')[:12000]}\n\n"
                "新文档必须达到相近的结构完整度、论述深度和案例粒度，但不得复制其主题内容。\n\n"
            )
        material = (
            f"【本篇标题】{title}\n【本篇目标】{goal}\n"
            f"【用户总任务】{task.get('instruction') or ''}\n\n"
            f"【当前岗位】{track.get('company') or ''} · {track.get('role') or track.get('target') or ''}\n\n"
            f"【需要继续修改或合并的候选】\n{source_text}\n\n"
            f"{target_text}{quality_text}【已确认资料】\n{packed['text']}"
        )
        raw = ai.chat(
            [{"role": "user", "content": material}], system=KNOWLEDGE_SPACE_EDITOR_PROMPT,
            max_tokens=4200, provider=model_selection["provider"],
        )
        _record_agent_actual_model(task["id"], run_id)
        summary, generated_title, document = _parse_agent_document(raw, title)
        reference_length = len((quality_reference or {}).get("proposed_content") or "")
        if reference_length >= 1000 and len(document) < max(800, int(reference_length * .55)):
            db.create_agent_event({
                "task_id": task["id"], "run_id": run_id, "event_type": "quality_retry",
                "label": f"《{title}》深度不足，自动补全", "detail": "未达到同组首篇文档的质量基准", "status": "running",
            })
            strengthened = material + (
                f"\n\n【质量校验】初稿仅 {len(document)} 字，同组基准文档约 {reference_length} 字。"
                "请重写为完整版，补齐核心原理、产品判断、办公 Agent 案例、边界与常见误区、面试可口述表达。"
            )
            raw = ai.chat([{"role": "user", "content": strengthened}], system=KNOWLEDGE_SPACE_EDITOR_PROMPT,
                          max_tokens=5200, provider=model_selection["provider"])
            _record_agent_actual_model(task["id"], run_id)
            summary, generated_title, document = _parse_agent_document(raw, title)
            if len(document) < max(800, int(reference_length * .55)):
                raise ValueError("文档深度仍未达到同组质量基准")
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        if existing_change_id:
            db.materialize_planned_proposed_change(
                existing_change_id, run_id=run_id, title=generated_title, content=document,
                reason=summary, metadata_json=metadata_json,
            )
        else:
            db.create_proposed_change({
                "task_id": task["id"], "run_id": run_id, "action_type": "create_track_knowledge",
                "target_type": "knowledge_item", "parent_type": "job_track", "parent_id": track["id"],
                "proposed_title": generated_title, "proposed_content": document, "reason": summary,
                "scope_type": "track", "status": "pending", "metadata_json": metadata_json,
            })
        db.update_agent_run(run_id, status="ready", summary=summary)
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "candidate_ready",
            "label": f"《{generated_title}》候选已完成", "detail": "等待独立确认", "status": "done",
        })
        return True
    except Exception:
        db.update_agent_run(run_id, status="failed", summary="本篇生成未完成")
        if existing_change_id:
            db.edit_proposed_change(
                existing_change_id, proposed_title=title, proposed_content="",
                reason="本篇生成未完成，可单独重试",
            )
            db.update_proposed_change(existing_change_id, "failed")
        else:
            db.create_proposed_change({
                "task_id": task["id"], "run_id": run_id, "action_type": "create_track_knowledge",
                "target_type": "knowledge_item", "parent_type": "job_track", "parent_id": track["id"],
                "proposed_title": title, "proposed_content": "", "reason": "本篇生成未完成，可单独重试",
                "scope_type": "track", "status": "failed",
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
            })
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "document_failed",
            "label": f"《{title}》生成未完成", "detail": "其他已完成候选不受影响，可只重试本篇",
            "status": "failed",
        })
        return False


def _run_track_multi_knowledge_task(task: dict, structured_documents=None) -> dict:
    track = db.get_job_track(task.get("track_id"))
    if not track:
        raise ValueError("知识任务对应的岗位已经不存在")
    folders, _created = db.ensure_track_knowledge_folders(track["id"])
    context = _json_loads_safe(task.get("context_json"), {}) or {}
    preferred = context.get("folder_name") or "专业知识"
    folder = next((item for item in folders if item.get("name") == preferred), None) or folders[0]
    expert = db.get_agent_expert("knowledge_coach") or {}
    selection = _expert_model_selection(expert)
    plan_run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "knowledge_plan", "expert_key": "knowledge_coach",
        **_agent_run_model_fields(selection), "status": "running",
        "input_json": json.dumps(context, ensure_ascii=False),
    })
    _record_agent_model(task["id"], plan_run_id, selection)
    db.update_agent_task(task["id"], status="active")
    assembled = caddie_context.build_context(
        track_id=track["id"], intent="knowledge_coach", query=task.get("instruction") or "",
    )
    packed = harness.pack_context(assembled.get("text") or "", task, budget=28000)
    if structured_documents is None:
        planner = """把用户的多文档知识任务拆成 2-5 篇互不重叠、可独立阅读的文档。标题简洁，goal 明确每篇范围。只返回 JSON：{\"documents\":[{\"title\":\"...\",\"goal\":\"...\"}]}"""
        plan_raw = ai.chat(
            [{"role": "user", "content": task.get("instruction") or ""}], system=planner,
            max_tokens=1000, provider=selection["provider"],
        )
        _record_agent_actual_model(task["id"], plan_run_id)
        plan = ai.extract_json(plan_raw)
        documents = (plan.get("documents") or [])[:5] if isinstance(plan, dict) else []
    else:
        documents = list(structured_documents)[:8]
    documents = [item for item in documents if isinstance(item, dict) and item.get("title")]
    minimum_documents = 1 if structured_documents is not None else 2
    if len(documents) < minimum_documents:
        raise ValueError("没有形成有效的文档计划")
    db.update_agent_run(
        plan_run_id, status="ready", summary=f"已规划 {len(documents)} 篇文档",
        output_json=json.dumps({"documents": documents}, ensure_ascii=False),
    )
    db.create_agent_event({
        "task_id": task["id"], "run_id": plan_run_id, "event_type": "plan",
        "label": f"已拆分为 {len(documents)} 篇文档", "detail": "将逐篇生成并独立保存候选状态",
        "status": "done",
    })
    planned = []
    for item in documents:
        metadata = {
            "track_id": track["id"], "folder_id": folder["id"], "folder_name": folder.get("name"),
            "folder_key": folder.get("folder_key"), "topic": item.get("title"),
            "source_expert": "knowledge_coach", "plan_title": item.get("title"),
            "plan_goal": item.get("goal"),
            "source_candidate_ids": item.get("source_candidate_ids") or [],
        }
        target_id = item.get("target_knowledge_item_id")
        target_document = db.get_knowledge_item(target_id) if target_id else None
        if target_document:
            metadata["base_hash"] = hashlib.sha256((target_document.get("content") or "").encode("utf-8")).hexdigest()
            metadata["original_title"] = target_document.get("title") or ""
        action_type = "update_knowledge" if target_document else "create_track_knowledge"
        change_id = db.create_proposed_change({
            "task_id": task["id"], "run_id": plan_run_id, "action_type": action_type,
            "target_type": "knowledge_item", "target_id": target_id,
            "parent_type": "job_track", "parent_id": track["id"],
            "proposed_title": item.get("title"), "proposed_content": "",
            "reason": item.get("goal"), "scope_type": "track", "status": "planned",
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        })
        planned.append((item, change_id))
    completed = sum(
        1 for item, change_id in planned
        if _generate_track_knowledge_plan_item(
            task, item, track, folder, packed, selection, existing_change_id=change_id,
        )
    )
    first_pass_failed = [
        (item, change_id) for item, change_id in planned
        if (db.get_proposed_change(change_id) or {}).get("status") == "failed"
    ]
    if first_pass_failed:
        db.create_agent_event({
            "task_id": task["id"], "run_id": plan_run_id, "event_type": "automatic_retry",
            "label": f"自动续跑 {len(first_pass_failed)} 篇未完成文档",
            "detail": "保留已完成候选，只重试未完成项", "status": "running",
        })
        for item, change_id in first_pass_failed:
            completed += int(_generate_track_knowledge_plan_item(
                task, item, track, folder, packed, selection, existing_change_id=change_id,
            ))
    failed = len(documents) - completed
    summary = f"已完成 {completed} 篇候选" + (f"，{failed} 篇可单独重试" if failed else "")
    db.update_agent_task(task["id"], status="review", result_summary=summary)
    return db.get_agent_task(task["id"])


EXPERT_ASSET_PROMPTS = {
    "resume_editor": """你是 Caddie 的简历编辑。基于岗位 JD、真实经历和当前简历文本，形成一份可执行的简历修改清单，而不是假装修改本地 Word/PDF。
必须包含：岗位筛选重点、保留/删除/前移建议、逐条候选 Bullet、每条证据来源、仍需用户确认的事实。不得编造数字或扩大用户贡献；证据不足时明确标为待确认。
正文是可长期保存的工作文档，不要写对话开场。""",
    "pressure_interviewer": """你是 Caddie 的压力面试官。基于当前岗位 JD、项目、知识和历史面试，设计一套真实的模拟面试方案。
必须包含：面试目标、问题顺序、每题考察意图、递进追问、评分标准和结束后的复盘维度。不要替用户回答问题；问题要能验证事实、逻辑、专业知识和临场表达。""",
    "review_analyst": """你是 Caddie 的复盘分析师。把用户提供的逐字稿、面试记录或复盘材料整理成深度报告。
必须包含：问答还原、面试官意图、回答证据引用、事实完整性、思维结构、语言表达、项目/专业深度、一次性失误、可训练能力、知识缺口和下一轮行动。没有逐字稿时明确证据不足，不得虚构问答。""",
}


ADVISORY_EXPERT_PROMPTS = {
    "career_lead": """你是 Caddie 的求职主理人。你的职责是判断用户当前真正要解决的问题，结合已有资料给出结论、依据和下一步，而不是只复述分工。
如果任务涉及多个环节，说明先后顺序及每一步的完成标准；如果资料不足，明确缺什么。不要声称其他专家已经执行，也不要虚构用户经历。""",
    "job_researcher": """你是 Caddie 的岗位研究员。基于当前岗位 JD、公司信息和用户真实经历，回答岗位要求、匹配点、差距、验证方式与准备优先级。
区分资料中已确认的事实和你的判断；没有外部资料时不要假装完成了联网调研。""",
}


def _requests_persistent_output(instruction: str) -> bool:
    text = re.sub(r"\s+", "", instruction or "")
    return any(marker in text for marker in (
        "整理入库", "写入", "保存", "存下来", "归档", "沉淀成文档",
        "生成文档", "形成报告", "更新进", "加入知识",
    ))


def _run_advisory_expert_task(task: dict, expert_key: str) -> dict:
    """Execute experts that answer and plan without directly writing user data."""
    _guard_agent_task(task, expert_key)
    expert = db.get_agent_expert(expert_key) or {}
    model_selection = _expert_model_selection(expert)
    track_id = task.get("track_id") or (
        task.get("object_id") if task.get("object_type") == "job_track" else None
    )
    project_id = task.get("object_id") if task.get("object_type") == "project" else None
    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "primary", "expert_key": expert_key,
        **_agent_run_model_fields(model_selection), "status": "running",
        "input_json": json.dumps({
            "track_id": track_id, "project_id": project_id,
            "instruction": task.get("instruction") or "",
        }, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    db.update_agent_task(task["id"], status="active")
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "context",
        "label": "读取当前资料", "detail": "正在读取岗位、经历、项目、反馈与既有知识",
        "status": "running",
    })
    if track_id:
        assembled = caddie_context.build_job_research_context(
            track_id=track_id, query=task.get("instruction") or "",
        )
    else:
        assembled = caddie_context.build_context(
            track_id=track_id, project_id=project_id, intent=expert_key,
            query=task.get("instruction") or "",
        )
    packed_context = harness.pack_context(assembled.get("text") or "", task, budget=28000)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "harness",
        "label": "Harness 已装配上下文",
        "detail": f"使用 {packed_context['used']} / {packed_context['budget']} 字；截断={packed_context['truncated']}",
        "status": "done",
        "payload_json": json.dumps(packed_context, ensure_ascii=False),
    })
    try:
        task_context = json.loads(task.get("context_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        task_context = {}
    delegates = task_context.get("delegate_experts") or []
    delegation_note = (
        "\n\n可供后续协作的专家：" + "、".join(delegates)
        if delegates else ""
    )
    material = (
        f"【用户任务】\n{task.get('instruction') or ''}\n\n"
        f"【用户经历概览】{_experience_context()}\n\n"
        f"【Caddie 已确认上下文】\n{packed_context['text']}"
        f"{delegation_note}"
    )
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "analysis",
        "label": "形成主理判断", "detail": "正在区分事实、判断、缺口与下一步",
        "status": "running",
    })
    answer = ai.chat(
        [{"role": "user", "content": material}],
        system=ADVISORY_EXPERT_PROMPTS[expert_key], max_tokens=2800,
        provider=model_selection["provider"],
    ).strip()
    _record_agent_actual_model(task["id"], run_id)
    _abort_if_agent_task_cancelled(task["id"])
    report = harness.audit_answer(answer, expert_key, mode="advisory")
    _record_harness_report(task["id"], run_id, report)
    if _harness_needs_auto_repair(report) and report.repair_instruction:
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "harness_repair",
            "label": "Harness 触发自动返修",
            "detail": "首版答复未过质量门，正在要求模型补齐结构、证据边界和下一步",
            "status": "running",
        })
        answer = ai.chat(
            [{"role": "user", "content": material + "\n\n【上一版输出】\n" + answer + "\n\n" + report.repair_instruction}],
            system=ADVISORY_EXPERT_PROMPTS[expert_key], max_tokens=3200,
            provider=model_selection["provider"],
        ).strip()
        _record_agent_actual_model(task["id"], run_id)
        _abort_if_agent_task_cancelled(task["id"])
        report = harness.audit_answer(answer, expert_key, mode="advisory_repair")
        _record_harness_report(task["id"], run_id, report)
    if not answer:
        raise ValueError("专家没有生成有效答复")
    write_changes = 0
    if _requests_persistent_output(task.get("instruction") or ""):
        title_prefix = "岗位研究" if expert_key == "job_researcher" else "求职推进"
        title = f"{title_prefix}：{task.get('title') or '本轮结论'}"[:100]
        db.create_proposed_change({
            "task_id": task["id"], "run_id": run_id, "action_type": "create_asset",
            "target_type": "asset", "proposed_title": title,
            "proposed_content": answer, "reason": "用户明确要求沉淀本轮分析",
            "scope_type": "track" if track_id else "global", "status": "pending",
            "metadata_json": json.dumps({
                "asset_type": "job_research" if expert_key == "job_researcher" else "career_plan",
                "track_id": track_id, "source_expert": expert_key,
            }, ensure_ascii=False),
        })
        write_changes = 1
    output = {"answer": answer, "write_changes": write_changes}
    db.update_agent_run(
        run_id, status="ready" if write_changes else "completed",
        output_json=json.dumps(output, ensure_ascii=False),
        summary=answer,
    )
    db.update_agent_task(
        task["id"], status="review" if write_changes else "completed", result_summary=answer
    )
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id,
        "event_type": "review" if write_changes else "completed",
        "label": "候选文档已就绪" if write_changes else "分析答复已完成",
        "detail": "等待用户确认后归档" if write_changes else "本轮只回答和规划，没有直接写入资料",
        "status": "done",
    })
    return db.get_agent_task(task["id"])


def _run_expert_asset_task(task: dict, expert_key: str) -> dict:
    """Run an expert and create one reviewable track asset candidate."""
    _guard_agent_task(task, expert_key)
    expert = db.get_agent_expert(expert_key) or {}
    model_selection = _expert_model_selection(expert)
    track_id = task.get("track_id")
    resume = None
    if expert_key == "resume_editor":
        resume_id = task.get("object_id") if task.get("object_type") == "resume_version" else None
        resume = db.get_resume_version(resume_id) if resume_id else None
        if not resume:
            recent = db.list_recent_resume_versions(limit=1)
            resume = recent[0] if recent else None
        track_id = track_id or (resume.get("track_id") if resume else None)
    track = db.get_job_track(track_id) if track_id else None
    if not track:
        if expert_key == "resume_editor":
            raise ValueError("简历编辑需要岗位范围：请先在工作区顶部选择目标岗位；如果还没有简历版本，请到该岗位详情登记或上传简历。")
        if expert_key == "review_analyst":
            raise ValueError("面试复盘需要知道对应岗位：请先选择求职线，并从该岗位的面试记录进入复盘。")
        raise ValueError("模拟面试需要绑定目标岗位：请先在工作区顶部选择岗位，再开始压力面试。")

    run_id = db.create_agent_run({
        "task_id": task["id"], "run_type": "primary", "expert_key": expert_key,
        **_agent_run_model_fields(model_selection), "status": "running",
        "input_json": json.dumps({
            "track_id": track_id, "object_type": task.get("object_type"),
            "object_id": task.get("object_id"), "instruction": task.get("instruction") or "",
        }, ensure_ascii=False),
    })
    _record_agent_model(task["id"], run_id, model_selection)
    db.update_agent_task(task["id"], status="active")
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "context",
        "label": "读取岗位档案", "detail": "正在读取 JD、真实经历、知识、简历版本和面试记录",
        "status": "running",
    })
    round_id = task.get("object_id") if task.get("object_type") == "interview_round" else None
    if expert_key == "review_analyst" and round_id:
        assembled = caddie_context.build_interview_round_context(
            round_id, intent=expert_key, query=task.get("instruction") or "",
        )
    elif track_id:
        assembled = caddie_context.build_job_research_context(
            track_id=track_id, query=task.get("instruction") or "",
        )
    else:
        assembled = caddie_context.build_context(
            track_id=track_id, intent=expert_key, query=task.get("instruction") or "",
        )
    packed_context = harness.pack_context(assembled.get("text") or "", task, budget=26000)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "harness",
        "label": "Harness 已装配上下文",
        "detail": f"使用 {packed_context['used']} / {packed_context['budget']} 字；截断={packed_context['truncated']}",
        "status": "done",
        "payload_json": json.dumps(packed_context, ensure_ascii=False),
    })
    resume_material = ""
    if resume:
        resume_material = (
            f"\n\n【当前简历版本】{resume.get('version_name') or ''}"
            f"\nWord：{resume.get('docx_path') or '未登记'}"
            f"\nPDF：{resume.get('pdf_path') or '未登记'}"
            f"\n版本说明：{resume.get('change_summary') or '无'}"
            f"\n提取文本：\n{(resume.get('extracted_text') or '')[:18000]}"
        )
    material = (
        f"【用户任务】\n{task.get('instruction') or ''}\n\n"
        f"【岗位】{track.get('company') or ''} · {track.get('role') or track.get('target') or ''}\n\n"
        f"【Caddie 已确认上下文】\n{packed_context['text']}"
        f"{resume_material}"
    )
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "analysis",
        "label": "形成专业判断", "detail": "正在区分已确认事实、推断、证据缺口与可执行修改",
        "status": "running",
    })
    system = EXPERT_ASSET_PROMPTS[expert_key] + """
严格使用下列分隔符返回，不要 JSON，不要代码围栏：
SUMMARY: 一句话结论
TITLE: 产出标题
===DOCUMENT===
完整 Markdown 正文
"""
    raw = ai.chat(
        [{"role": "user", "content": material}], system=system, max_tokens=4200,
        provider=model_selection["provider"],
    )
    _record_agent_actual_model(task["id"], run_id)
    _abort_if_agent_task_cancelled(task["id"])
    fallback_titles = {
        "resume_editor": f"{track.get('company')} · {track.get('role')} 简历修改清单",
        "pressure_interviewer": f"{track.get('company')} · {track.get('role')} 模拟面试方案",
        "review_analyst": f"{track.get('company')} · {track.get('role')} 面试复盘报告",
    }
    summary, title, document = _parse_agent_document(raw, fallback_titles[expert_key])
    report = harness.audit_document(title, document, expert_key)
    _record_harness_report(task["id"], run_id, report)
    if _harness_needs_auto_repair(report) and report.repair_instruction:
        db.create_agent_event({
            "task_id": task["id"], "run_id": run_id, "event_type": "harness_repair",
            "label": "Harness 触发自动返修",
            "detail": "首版资产未过质量门，正在要求模型补齐结构、证据边界和行动项",
            "status": "running",
        })
        raw = ai.chat(
            [{"role": "user", "content": material + "\n\n【上一版输出】\n" + raw + "\n\n" + report.repair_instruction}],
            system=system, max_tokens=4600, provider=model_selection["provider"],
        )
        _record_agent_actual_model(task["id"], run_id)
        _abort_if_agent_task_cancelled(task["id"])
        summary, title, document = _parse_agent_document(raw, fallback_titles[expert_key])
        report = harness.audit_document(title, document, expert_key)
        _record_harness_report(task["id"], run_id, report)
    asset_types = {
        "resume_editor": "resume_edit_plan",
        "pressure_interviewer": "mock_interview_plan",
        "review_analyst": "review",
    }
    db.create_proposed_change({
        "task_id": task["id"], "run_id": run_id, "action_type": "create_asset",
        "target_type": "asset", "proposed_title": title,
        "proposed_content": document, "reason": summary, "scope_type": "track",
        "status": "pending", "metadata_json": json.dumps({
            "asset_type": asset_types[expert_key], "track_id": track_id,
            "source_expert": expert_key, "resume_version_id": resume.get("id") if resume else None,
        }, ensure_ascii=False),
    })
    output = {"summary": summary, "title": title, "asset_type": asset_types[expert_key]}
    db.update_agent_run(run_id, status="ready", output_json=json.dumps(output, ensure_ascii=False), summary=summary)
    db.update_agent_task(task["id"], status="review", result_summary=summary)
    db.create_agent_event({
        "task_id": task["id"], "run_id": run_id, "event_type": "review",
        "label": "候选产出已就绪", "detail": "尚未归档，等待用户在右侧确认",
        "status": "done",
    })
    return db.get_agent_task(task["id"])


def _run_agent_task(task_id: int):
    task = db.get_agent_task(task_id)
    if not task:
        return
    try:
        try:
            task_context = json.loads(task.get("context_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            task_context = {}
        expert_key = task.get("assigned_expert")
        if expert_key in {"career_lead", "job_researcher"}:
            _run_advisory_expert_task(task, expert_key)
            return
        if expert_key == "experience_detective":
            _run_experience_detective_task(task)
            return
        if expert_key == "knowledge_coach":
            dispatch = task_context.get("dispatch") or {}
            if dispatch and task.get("track_id"):
                if dispatch.get("intent") == "retry_failed_document":
                    raise ValueError("失败项应通过原任务的重试操作恢复")
                _run_track_multi_knowledge_task(task, dispatch.get("document_plan") or [])
                return
            if task.get("object_type") == "knowledge_item":
                _run_standalone_knowledge_task(task)
                return
            track_id = task.get("track_id") or (task.get("object_id") if task.get("object_type") == "job_track" else None)
            if not track_id:
                _run_global_knowledge_task(task)
                return
            if task.get("task_type") == "knowledge_create":
                _run_track_single_knowledge_task(task)
                return
            expert = db.get_agent_expert("knowledge_coach") or {}
            model_selection = _expert_model_selection(expert)
            run_id = db.create_agent_run({
                "task_id": task_id, "run_type": "primary", "expert_key": "knowledge_coach",
                **_agent_run_model_fields(model_selection), "status": "running",
                "input_json": json.dumps({"instruction": task.get("instruction")}, ensure_ascii=False),
            })
            _record_agent_model(task_id, run_id, model_selection)
            legacy_id = db.create_knowledge_agent_run({
                "task_id": task_id, "agent_run_id": run_id, "track_id": track_id,
                "instruction": task.get("instruction") or "", "status": "planning",
                "current_document_id": task_context.get("current_document_id"),
            })
            db.update_agent_task(task_id, status="active")
            _run_knowledge_space_agent(
                track_id, task.get("instruction") or "",
                task_context.get("current_document_id"), legacy_id,
                model_provider=model_selection["provider"],
            )
            return
        if expert_key in {"resume_editor", "pressure_interviewer", "review_analyst"}:
            _run_expert_asset_task(task, expert_key)
            return
        raise ValueError(f"专家「{expert_key or '未分配'}」尚未接入执行器")
    except Exception as exc:
        current = db.get_agent_task(task_id)
        if current and current.get("status") not in {"review", "completed", "cancelled"}:
            for run in current.get("runs") or []:
                if run.get("status") in {"queued", "running"}:
                    db.update_agent_run(run["id"], status="failed", summary=str(exc)[:300])
            db.update_agent_task(
                task_id, status="failed",
                result_summary="模型服务暂时不可用。可以重试失败项或切换模型后重试；已完成候选不会丢失。",
            )
            db.create_agent_event({
                "task_id": task_id, "event_type": "failed", "label": "任务执行失败",
                "detail": "模型服务暂时不可用，请重试失败项；已完成候选会继续保留。",
                "status": "failed",
            })


def _recover_knowledge_coach_tasks():
    """Resume durable knowledge tasks without discarding completed candidates."""
    for task_type in ("knowledge_create", "knowledge_edit", "knowledge_orchestrated"):
        for task in db.list_agent_tasks_for_recovery(task_type):
            full_task = db.get_agent_task(task["id"]) or task
            incomplete = [
                item for item in (full_task.get("changes") or [])
                if item.get("status") in {"planned", "generating"}
            ]
            for item in incomplete:
                db.edit_proposed_change(
                    item["id"], proposed_content="",
                    reason="服务重启前本篇尚未完成，可单独重试",
                )
                db.update_proposed_change(item["id"], "failed")
            if incomplete:
                full_task = db.get_agent_task(task["id"]) or full_task
            pending = [item for item in (full_task.get("changes") or []) if item.get("status") == "pending"]
            failed = [item for item in (full_task.get("changes") or []) if item.get("status") == "failed"]
            if pending or failed:
                db.update_agent_task(task["id"], status="review",
                                     result_summary=(
                                         f"服务重启后已恢复 {len(pending)} 篇待确认候选"
                                         + (f"，{len(failed)} 篇可单独重试" if failed else "")
                                     ))
                continue
            for run in full_task.get("runs") or []:
                if run.get("status") in {"queued", "running"}:
                    db.update_agent_run(run["id"], status="failed",
                                        summary="服务重启，正在重新执行未完成项")
            db.update_agent_task(task["id"], status="queued",
                                 result_summary="服务重启，正在恢复知识任务")
            db.create_agent_event({
                "task_id": task["id"], "event_type": "recovered",
                "label": "服务重启后恢复知识任务",
                "detail": "只重新执行未完成项，已有候选保持不变", "status": "running",
            })
            _run_agent_task(task["id"])


@app.post("/api/agent/tasks/{task_id}/run")
def run_agent_task(task_id: int, background_tasks: BackgroundTasks):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    if task.get("status") in {"active", "review", "completed"}:
        raise HTTPException(409, "这个任务已经开始或完成")
    if task.get("status") == "cancelled":
        raise HTTPException(409, "已取消的任务不能执行")
    db.update_agent_task(task_id, status="queued")
    db.create_agent_event({
        "task_id": task_id, "event_type": "queued", "label": "已进入执行队列",
        "detail": f"等待「{(db.get_agent_expert(task.get('assigned_expert')) or {}).get('name') or task.get('assigned_expert')}」开始处理",
        "status": "pending",
    })
    background_tasks.add_task(_run_agent_task, task_id)
    return _agent_task_payload(db.get_agent_task(task_id))


@app.post("/api/agent/tasks/{task_id}/retry-failed")
def retry_failed_knowledge_items(task_id: int, body: AgentTaskRetryFailedIn):
    task = db.get_agent_task(task_id)
    if not task or task.get("assigned_expert") != "knowledge_coach" or not task.get("track_id"):
        raise HTTPException(404, "知识任务不存在")
    failed = [item for item in (task.get("changes") or []) if item.get("status") == "failed"]
    if body.change_ids:
        wanted = set(body.change_ids)
        failed = [item for item in failed if item.get("id") in wanted]
    if not failed:
        raise HTTPException(400, "没有可重试的失败项")
    track = db.get_job_track(task["track_id"])
    folders, _created = db.ensure_track_knowledge_folders(track["id"])
    context = _json_loads_safe(task.get("context_json"), {}) or {}
    preferred = context.get("folder_name") or "专业知识"
    folder = next((item for item in folders if item.get("name") == preferred), None) or folders[0]
    selection = _expert_model_selection(db.get_agent_expert("knowledge_coach") or {})
    assembled = caddie_context.build_context(
        track_id=track["id"], intent="knowledge_coach", query=task.get("instruction") or "",
    )
    packed = harness.pack_context(assembled.get("text") or "", task, budget=28000)
    db.update_agent_task(task_id, status="active", result_summary="正在只重试失败项")
    completed = 0
    for change in failed:
        metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
        plan_item = {
            "title": metadata.get("plan_title") or change.get("proposed_title"),
            "goal": metadata.get("plan_goal") or "完成这篇知识文档",
        }
        completed += int(_generate_track_knowledge_plan_item(
            task, plan_item, track, folder, packed, selection, existing_change_id=change["id"],
        ))
    remaining = len([x for x in (db.get_agent_task(task_id).get("changes") or []) if x.get("status") == "failed"])
    summary = f"本次重试完成 {completed} 篇" + (f"，仍有 {remaining} 篇未完成" if remaining else "")
    db.update_agent_task(task_id, status="review", result_summary=summary)
    return _agent_task_payload(db.get_agent_task(task_id))


def _pending_agent_change(task_id: int, change_id: int) -> tuple[dict, dict]:
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    change = next((item for item in (task.get("changes") or []) if item.get("id") == change_id), None)
    if not change:
        raise HTTPException(404, "候选修改不存在")
    if change.get("status") != "pending":
        raise HTTPException(409, "这条候选已经处理，不能再次编辑或拒绝")
    return task, change


def _change_scope_id(task: dict, change: dict):
    if change.get("scope_type") == "project":
        return change.get("parent_id") or change.get("target_id")
    return task.get("track_id")


@app.patch("/api/agent/tasks/{task_id}/changes/{change_id}")
def edit_agent_task_change(task_id: int, change_id: int, body: AgentProposalEditIn):
    """Let the human correct a proposal before applying it; no source asset is changed yet."""
    task, change = _pending_agent_change(task_id, change_id)
    if body.proposed_title is not None and not body.proposed_title.strip():
        raise HTTPException(400, "候选标题不能为空")
    if body.proposed_content is not None and not body.proposed_content.strip():
        raise HTTPException(400, "候选内容不能为空")
    changed = db.edit_proposed_change(
        change_id, proposed_title=body.proposed_title.strip() if body.proposed_title is not None else None,
        proposed_content=body.proposed_content.strip() if body.proposed_content is not None else None,
        reason=body.reason.strip() if body.reason is not None else None,
    )
    if not changed:
        raise HTTPException(400, "没有可保存的候选修改")
    db.create_agent_event({
        "task_id": task_id, "run_id": change.get("run_id"), "event_type": "review_edited",
        "label": "用户编辑了 Agent 候选", "detail": change.get("proposed_title") or "候选修改",
        "status": "done", "payload_json": json.dumps({"change_id": change_id}, ensure_ascii=False),
    })
    _workspace_event("agent_candidate_edited", "proposed_change", change_id,
                     "用户编辑了 Agent 候选", change.get("scope_type") or "global", _change_scope_id(task, change),
                     payload={"task_id": task_id})
    _commit("用户编辑 Agent 候选修改")
    return _agent_update_package_payload(task_id)


@app.post("/api/agent/tasks/{task_id}/changes/{change_id}/reject")
def reject_agent_task_change(task_id: int, change_id: int, body: AgentProposalRejectIn):
    """Reject a candidate with a reason future Agents can retrieve through change events."""
    task, change = _pending_agent_change(task_id, change_id)
    reason = body.reason.strip() or "用户暂不采纳该候选"
    db.update_proposed_change(change_id, "ignored")
    db.create_agent_event({
        "task_id": task_id, "run_id": change.get("run_id"), "event_type": "review_rejected",
        "label": "用户拒绝了 Agent 候选", "detail": reason, "status": "done",
        "payload_json": json.dumps({"change_id": change_id, "reason": reason}, ensure_ascii=False),
    })
    _workspace_event("agent_candidate_rejected", "proposed_change", change_id,
                     "用户拒绝了 Agent 候选", change.get("scope_type") or "global", _change_scope_id(task, change),
                     payload={"task_id": task_id, "reason": reason})
    _commit("用户拒绝 Agent 候选修改")
    return _agent_update_package_payload(task_id)


@app.post("/api/agent/tasks/{task_id}/apply")
def apply_agent_task(task_id: int, body: AgentTaskApplyIn):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, "Agent 任务不存在")
    valid = {x["id"]: x for x in task.get("changes") or [] if x.get("status") == "pending"}
    selected = [valid[cid] for cid in body.change_ids if cid in valid]
    if not selected:
        requested = [db.get_proposed_change(cid) for cid in body.change_ids]
        requested = [x for x in requested if x]
        if requested and any(int(x.get("task_id") or 0) != task_id for x in requested):
            raise HTTPException(409, "页面候选已更新，请刷新后再次确认")
        if requested and all(x.get("status") == "applied" for x in requested):
            raise HTTPException(409, "所选文档已经写入，无需重复确认")
        if requested and any(x.get("status") == "failed" for x in requested):
            raise HTTPException(409, "所选文档尚未生成完成，请先重试")
        raise HTTPException(400, "当前没有可确认的待写入文档")

    legacy = [db.legacy_knowledge_change_for_proposal(x["id"]) for x in selected]
    legacy = [x for x in legacy if x]
    if legacy:
        run_ids = {x["run_id"] for x in legacy}
        if len(run_ids) != 1 or len(legacy) != len(selected):
            raise HTTPException(400, "这组候选修改不能一起应用")
        results = db.apply_knowledge_agent_run(next(iter(run_ids)), [x["id"] for x in legacy])
        _commit(f"知识教练应用候选修改：{len(results)} 项")
        return {"ok": True, "results": results, "task": _agent_task_payload(db.get_agent_task(task_id))}

    supported = {
        "create_followup", "update_followup", "create_global_knowledge",
        "create_track_knowledge", "update_knowledge", "update_project_document",
        "create_asset", "create_fact", "create_feedback", "update_job_record",
        "change_interview_schedule",
    }
    for change in selected:
        action = change.get("action_type")
        if action not in supported:
            raise HTTPException(400, f"暂不支持应用动作：{action}")
        if action == "create_followup":
            if change.get("parent_type") != "project" or not db.get_project(change.get("parent_id")):
                raise HTTPException(400, "候选修改对应的项目已经不存在")
        elif action == "update_followup":
            current = db.get_followup(change.get("target_id"))
            if change.get("target_type") != "followup" or not current:
                raise HTTPException(400, "候选修改对应的追问已经不存在")
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            base_hash = metadata.get("base_answer_hash")
            current_answer = current.get("answer") or ""
            if base_hash and hashlib.sha256(current_answer.encode("utf-8")).hexdigest() != base_hash:
                raise HTTPException(409, "追问回答在候选生成后已经更新，请基于最新版重新生成")
        elif action == "create_global_knowledge":
            if not (change.get("proposed_title") or "").strip() or not (change.get("proposed_content") or "").strip():
                raise HTTPException(400, "通用知识文档缺少标题或正文")
        elif action == "create_track_knowledge":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            track_id = metadata.get("track_id") or change.get("parent_id")
            folder = db.get_knowledge_folder(metadata.get("folder_id")) if metadata.get("folder_id") else None
            if (change.get("parent_type") != "job_track" or not db.get_job_track(track_id) or
                    not folder or folder.get("scope_type") != "track" or folder.get("track_id") != track_id):
                raise HTTPException(400, "候选修改对应的岗位准备目录已经不存在")
            if not (change.get("proposed_title") or "").strip() or not (change.get("proposed_content") or "").strip():
                raise HTTPException(400, "岗位准备文档缺少标题或正文")
        elif action == "create_asset":
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if metadata.get("track_id") and not db.get_job_track(metadata.get("track_id")):
                raise HTTPException(400, "候选产出对应的岗位已经不存在")
        elif action == "create_fact":
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not metadata.get("subject_type") or not metadata.get("predicate") or not (change.get("proposed_content") or "").strip():
                raise HTTPException(400, "事实候选缺少主体、字段或内容")
        elif action == "create_feedback":
            if not (change.get("proposed_content") or "").strip():
                raise HTTPException(400, "反馈候选内容为空")
        elif action == "update_job_record":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            current_track = db.get_job_track(metadata.get("track_id") or change.get("target_id"))
            if not current_track:
                raise HTTPException(400, "候选修改对应的岗位已经不存在")
            if (metadata.get("base_track_updated_at")
                    and current_track.get("updated_at") != metadata.get("base_track_updated_at")):
                raise HTTPException(409, "岗位记录在候选生成后已经更新，请让 Agent 重新读取后再提交")
            application_id = metadata.get("application_id")
            if application_id:
                current_application = db.get_application(application_id)
                if not current_application:
                    raise HTTPException(409, "候选修改对应的投递记录已经不存在")
                if (metadata.get("base_application_updated_at")
                        and current_application.get("updated_at") != metadata.get("base_application_updated_at")):
                    raise HTTPException(409, "投递记录在候选生成后已经更新，请让 Agent 重新读取后再提交")
        elif action == "change_interview_schedule":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            if not db.get_job_track(metadata.get("track_id") or change.get("parent_id")):
                raise HTTPException(400, "候选修改对应的岗位已经不存在")
            operation = metadata.get("operation")
            if operation not in {"schedule", "update", "delete"}:
                raise HTTPException(400, "面试安排候选的操作类型无效")
            if operation in {"update", "delete"}:
                current_interview = db.get_interview(metadata.get("interview_id"))
                if not current_interview:
                    raise HTTPException(409, "候选修改对应的面试安排已经不存在")
                if current_interview.get("track_id") != metadata.get("track_id"):
                    raise HTTPException(409, "面试安排已经不属于原岗位，请重新核对")
                if (metadata.get("base_interview_updated_at")
                        and current_interview.get("updated_at") != metadata.get("base_interview_updated_at")):
                    raise HTTPException(409, "面试安排在候选生成后已经更新，请让 Agent 重新读取后再提交")
        elif action == "update_knowledge":
            current = db.get_knowledge_item(change.get("target_id"))
            if change.get("target_type") != "knowledge_item" or not current:
                raise HTTPException(400, "候选修改对应的知识文档已经不存在")
        else:
            current = db.get_project(change.get("target_id"))
            if change.get("target_type") != "project" or not current:
                raise HTTPException(400, "候选修改对应的项目文档已经不存在")
        if action in {"update_knowledge", "update_project_document"}:
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            base_hash = metadata.get("base_hash")
            current_content = (current.get("content") if action == "update_knowledge"
                               else current.get("document")) or ""
            if base_hash and hashlib.sha256(current_content.encode("utf-8")).hexdigest() != base_hash:
                raise HTTPException(409, "原文在候选生成后已经更新，请让 Agent 基于最新版重新生成，避免覆盖新内容")

    results = []
    applied_labels = []
    for change in selected:
        if change.get("action_type") == "update_job_record":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            track_id = metadata.get("track_id") or change.get("target_id")
            changes = metadata.get("changes") or {}
            current_track = db.get_job_track(track_id)
            track_fields = {
                "company", "role", "target", "priority", "notes", "persona", "apply_url",
            }
            track_updates = {key: value for key, value in changes.items() if key in track_fields}
            if "track_status" in changes:
                track_updates["status"] = changes["track_status"]
            if track_updates:
                db.update_job_track(track_id, {**current_track, **track_updates})
            application_fields = {"applied_date", "source", "remark_tag", "evaluation"}
            app_updates = {key: value for key, value in changes.items() if key in application_fields}
            needs_application = bool(app_updates or changes.get("application_status"))
            application = db.get_application(metadata.get("application_id")) if metadata.get("application_id") else None
            if needs_application and not application:
                application, _created = db.ensure_track_application(track_id, {
                    "status": changes.get("application_status") or "applied",
                    "applied_date": changes.get("applied_date"),
                    "source": changes.get("source") or "Agent 确认",
                    "remark_tag": changes.get("remark_tag"),
                    "evaluation": changes.get("evaluation"),
                })
            elif application and app_updates:
                db.update_application(application["id"], {**application, **app_updates})
                application = db.get_application(application["id"])
            if application and changes.get("application_status"):
                db.set_application_status(application["id"], changes["application_status"], allow_skip=True)
            db.update_proposed_change(change["id"], "applied", target_id=track_id)
            _workspace_event(
                "job_record_confirmed", "job_track", track_id,
                "确认岗位事务修改", "track", track_id,
                actor_type="user", payload={"task_id": task_id, "changes": changes},
            )
            results.append({"change_id": change["id"], "track_id": track_id,
                            "application_id": application.get("id") if application else None})
            applied_labels.append("岗位记录")
            continue

        if change.get("action_type") == "change_interview_schedule":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            track_id = metadata.get("track_id") or change.get("parent_id")
            operation = metadata.get("operation")
            values = metadata.get("values") or {}
            application = db.get_application(metadata.get("application_id")) if metadata.get("application_id") else None
            if operation == "schedule":
                if not application:
                    application, _created = db.ensure_track_application(track_id, {"status": "interview"})
                interview_id = db.add_interview(application["id"], values)
                if application.get("status") != "interview":
                    db.set_application_status(application["id"], "interview", allow_skip=True)
            elif operation == "update":
                interview_id = metadata.get("interview_id")
                current_interview = db.get_interview(interview_id)
                db.update_interview(interview_id, {**current_interview, **values})
            else:
                interview_id = metadata.get("interview_id")
                db.delete_interview(interview_id)
            db.update_proposed_change(
                change["id"], "applied",
                target_id=interview_id if operation != "delete" else None,
            )
            _workspace_event(
                "interview_schedule_confirmed", "interview", interview_id,
                {"schedule": "确认新增面试", "update": "确认修改面试安排",
                 "delete": "确认删除错误面试"}[operation],
                "track", track_id, actor_type="user",
                payload={"task_id": task_id, "operation": operation},
            )
            results.append({"change_id": change["id"], "track_id": track_id,
                            "interview_id": interview_id, "operation": operation})
            applied_labels.append("面试安排")
            continue

        if change.get("action_type") == "create_followup":
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            fid = db.create_followup(change.get("parent_id"), {
                "question": change.get("proposed_title"),
                "answer": change.get("proposed_content") or None,
                "status": "todo",
                "category": metadata.get("category"), "kind": metadata.get("kind"),
                "origin": metadata.get("origin") or "agent",
                "source_track_id": task.get("track_id"),
            })
            db.update_proposed_change(change["id"], "applied", target_id=fid)
            _workspace_event("followup_confirmed", "followup", fid, "确认项目追问", "project", change.get("parent_id"))
            results.append({"change_id": change["id"], "followup_id": fid})
            applied_labels.append("项目追问")
            continue

        if change.get("action_type") == "update_followup":
            current = db.get_followup(change.get("target_id"))
            db.update_followup(current["id"], {
                **current,
                "question": (change.get("proposed_title") or current.get("question") or "").strip(),
                "answer": change.get("proposed_content") or "",
            })
            db.update_proposed_change(change["id"], "applied")
            _workspace_event("followup_confirmed", "followup", current["id"],
                             "确认追问回答更新", "project", current.get("project_id"))
            results.append({"change_id": change["id"], "followup_id": current["id"]})
            applied_labels.append("追问回答")
            continue

        if change.get("action_type") == "create_track_knowledge":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            knowledge_id = db.create_knowledge_item({
                "title": (change.get("proposed_title") or "未命名文档").strip(),
                "content": change.get("proposed_content") or "", "scope_type": "track",
                "track_id": metadata.get("track_id") or change.get("parent_id"),
                "folder_id": metadata.get("folder_id"), "topic": metadata.get("topic") or "岗位准备",
                "mastery": metadata.get("mastery") or "learning", "source_type": "external_agent",
                "source_ref_id": task_id, "status": "active",
            })
            db.update_proposed_change(change["id"], "applied", target_id=knowledge_id)
            _workspace_event("knowledge_confirmed", "knowledge_item", knowledge_id, "确认岗位准备文档",
                             "track", metadata.get("track_id") or change.get("parent_id"),
                             actor_type="user", payload={"folder_key": metadata.get("folder_key"), "task_id": task_id})
            results.append({
                "change_id": change["id"],
                **_knowledge_write_receipt(db.get_knowledge_item(knowledge_id), "create"),
            })
            applied_labels.append("岗位准备文档")
            continue

        if change.get("action_type") == "create_global_knowledge":
            metadata = _json_loads_safe(change.get("metadata_json"), {}) or {}
            knowledge_id = db.create_knowledge_item({
                "title": (change.get("proposed_title") or "未命名知识").strip(),
                "content": change.get("proposed_content") or "",
                "scope_type": "global", "track_id": None, "folder_id": None,
                "topic": metadata.get("topic") or change.get("proposed_title") or "通用知识",
                "mastery": metadata.get("mastery") or "learning",
                "source_type": "agent", "source_ref_id": task_id, "status": "active",
            })
            db.update_proposed_change(change["id"], "applied", target_id=knowledge_id)
            _workspace_event(
                "knowledge_confirmed", "knowledge_item", knowledge_id,
                "确认通用知识文档", "global", None,
                actor_type="user", payload={"task_id": task_id},
            )
            results.append({
                "change_id": change["id"],
                **_knowledge_write_receipt(db.get_knowledge_item(knowledge_id), "create"),
            })
            applied_labels.append("通用知识文档")
            continue

        if change.get("action_type") == "create_fact":
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            fact_id = db.create_career_fact({
                "subject_type": metadata.get("subject_type"), "subject_id": metadata.get("subject_id"),
                "scope_type": metadata.get("scope_type") or change.get("scope_type") or "global",
                "scope_id": metadata.get("scope_id"), "predicate": metadata.get("predicate"),
                "value_text": change.get("proposed_content") or "", "confidence": metadata.get("confidence"),
                "evidence_json": json.dumps(metadata.get("evidence") or [], ensure_ascii=False),
                "provenance_json": json.dumps({"agent_task_id": task_id, "proposed_change_id": change["id"],
                                                 "agent_key": metadata.get("agent_key")}, ensure_ascii=False),
                "state": "confirmed", "created_by": "user_confirmed_agent",
            })
            db.update_proposed_change(change["id"], "applied", target_id=fact_id)
            db.create_workspace_change_event({
                "event_type": "fact_confirmed", "target_type": "career_fact", "target_id": fact_id,
                "scope_type": metadata.get("scope_type") or change.get("scope_type") or "global",
                "scope_id": metadata.get("scope_id"), "actor_type": "user",
                "summary": f"确认事实：{metadata.get('predicate') or '未命名字段'}",
            })
            results.append({"change_id": change["id"], "career_fact_id": fact_id})
            applied_labels.append("事实")
            continue

        if change.get("action_type") == "create_feedback":
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            saved = _save_feedback_with_harness({
                "original_text": change.get("proposed_content") or "", "scope": metadata.get("scope") or "global",
                "scope_id": metadata.get("scope_id"), "category": metadata.get("category"),
                "polarity": metadata.get("polarity"), "strength": metadata.get("strength"),
                "directive": metadata.get("directive"), "apply_stale": True,
            })
            feedback_id = saved["feedback"]["id"]
            db.update_proposed_change(change["id"], "applied", target_id=feedback_id)
            db.create_workspace_change_event({
                "event_type": "feedback_confirmed", "target_type": "feedback_note", "target_id": feedback_id,
                "scope_type": metadata.get("scope") or "global", "scope_id": metadata.get("scope_id"),
                "actor_type": "user", "summary": "确认并启用反馈约束",
            })
            results.append({"change_id": change["id"], "feedback_id": feedback_id,
                            "stale_assets": saved["impact"].get("stale_changed", 0)})
            applied_labels.append("反馈约束")
            continue

        if change.get("action_type") == "update_knowledge":
            item = db.get_knowledge_item(change.get("target_id"))
            updated = {
                **item,
                "title": (change.get("proposed_title") or item.get("title") or "未命名知识").strip(),
                "content": change.get("proposed_content") or "",
            }
            db.update_knowledge_item(item["id"], updated)
            db.update_proposed_change(change["id"], "applied", target_id=item["id"])
            _workspace_event("knowledge_confirmed", "knowledge_item", item["id"], "确认知识文档修改",
                             item.get("scope_type") or "global", item.get("track_id"))
            results.append({
                "change_id": change["id"],
                **_knowledge_write_receipt(db.get_knowledge_item(item["id"]), "update"),
            })
            applied_labels.append("知识文档")
            continue

        if change.get("action_type") == "create_asset":
            try:
                metadata = json.loads(change.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            aid = db.create_asset({
                "asset_type": metadata.get("asset_type") or "other",
                "track_id": metadata.get("track_id") or task.get("track_id"),
                "title": change.get("proposed_title") or "Agent 产出",
                "body": change.get("proposed_content") or "", "status": "draft",
                "provenance_json": json.dumps({
                    "agent_task_id": task_id, "expert": metadata.get("source_expert"),
                    "resume_version_id": metadata.get("resume_version_id"),
                }, ensure_ascii=False),
            })
            db.update_proposed_change(change["id"], "applied", target_id=aid)
            _workspace_event("asset_confirmed", "asset", aid, "确认岗位成果", "track",
                             metadata.get("track_id") or task.get("track_id"))
            results.append({"change_id": change["id"], "asset_id": aid})
            applied_labels.append("岗位成果")
            continue

        project = db.get_project(change.get("target_id"))
        db.update_project(project["id"], {
            **project,
            "name": (change.get("proposed_title") or project.get("name") or "未命名项目").strip(),
            "document": change.get("proposed_content") or "",
        })
        db.update_proposed_change(change["id"], "applied")
        _workspace_event("project_confirmed", "project", project["id"], "确认项目文档修改", "project", project["id"])
        results.append({"change_id": change["id"], "project_id": project["id"]})
        applied_labels.append("项目文档")
    for run in task.get("runs") or []:
        if run.get("status") not in {"completed", "failed", "cancelled"}:
            db.update_agent_run(run["id"], status="completed")
    created_knowledge = next(
        (item for item in results if item.get("knowledge_item_id")
         and item.get("action_type") == "create"), None
    )
    refreshed_before_status = db.get_agent_task(task_id)
    remaining = [x for x in (refreshed_before_status.get("changes") or []) if x.get("status") in {"planned", "generating", "pending", "failed"}]
    if created_knowledge and task.get("conversation_id") and not remaining:
        db.promote_workspace_session_to_document(
            task["conversation_id"], created_knowledge["knowledge_item_id"],
            created_knowledge.get("title"),
        )
    db.update_agent_task(task_id, status="review" if remaining else "completed",
                         result_summary=(f"已写入 {len(results)} 篇，仍有 {len(remaining)} 篇待处理" if remaining else f"已写入 {len(results)} 篇"))
    db.create_agent_event({
        "task_id": task_id, "event_type": "applied", "label": "已应用确认的修改",
        "detail": f"已应用 {len(results)} 项修改" + (f"，保留 {len(remaining)} 项继续处理" if remaining else ""),
    })
    kinds = "、".join(dict.fromkeys(applied_labels))
    _commit(f"Agent 应用候选修改：{kinds}（{len(results)} 项）")
    return {"ok": True, "results": results, "task": _agent_task_payload(db.get_agent_task(task_id))}


KNOWLEDGE_AGENT_PROMPT = """你是 Caddie 的知识库协作 Agent。你不是只回答问题，而是和用户共同维护一篇可复用的知识文档。
请结合当前文档、它的复用范围和用户指令，给出一版完整的更新后 Markdown。
要求：保留正确且仍有价值的原文；补充或改写用户要求的部分；结构有层次；面向求职准备时应能理解、能口述、能迁移使用；不要编造事实。
严格使用下面的分隔符返回，不要 JSON，不要代码围栏：
SUMMARY: 一句话说明你改了什么
===DOCUMENT===
完整的更新后 Markdown
"""

KNOWLEDGE_SPACE_PLANNER_PROMPT = """你是 Caddie 的知识空间规划 Agent。用户会给你一个岗位知识空间目录、检索结果和任务。
先判断要读取和改动哪些文档，再给出最小必要的操作计划。不要写长篇正文。

规则：
0. 输入中的“完整岗位研究上下文”是已经真实读取的 Caddie 资料。不得声称没有 JD、个人经历或同公司岗位材料；只有该上下文明确标注缺失时，才允许追问用户。
1. 最多 5 个操作；只允许 update 或 create，不允许删除。
2. 每个操作必须给 scope_type：track（当前岗位专属）或 domain（行业/职能通用）。
3. 这是从某个“岗位准备空间”发起的任务；除非用户明确说“行业通用 / 跨岗位复用 / 保存到行业知识 / 专业知识库通用层”，默认 scope_type=track，放在当前岗位下。
4. 用户说“专业知识 / 面试准备 / 岗位资料 / 当前 JD / 这个岗位 / 这家公司 / 我的简历 / 面试话术 / 复盘 / 留存到当前准备”时，必须使用 track。
5. 只有纯概念、纯行业框架、纯职能方法，且用户明确希望跨岗位复用时，才使用 domain；domain 文档禁止写当前公司、当前 JD、个人经历、面试话术或当前岗位包装。
6. 如果仅凭用户指令无法判断放到 track 还是 domain，优先选择 track，并在 reason 里说明“先放当前岗位，确认后可提升到行业知识”；不要擅自把岗位入口的内容放进 domain。
7. update 可选择当前岗位文档，或与当前行业一致的 domain 文档；不要改个人通用和其他公司的知识。
8. 不为同一主题重复建文档；能更新已有文档就更新。
9. track 操作的 folder_name 必须使用目录清单里的名称；domain 操作可留空，并填写精确 domain_key（如“基金”“固收”“资管”“互联网产品”），不要笼统写“金融相关”。
10. 用户提出“系统讲解、全面梳理、知识体系”等宽主题时，不要把所有内容塞进一篇大文档。先按互不重叠的知识维度拆成 3-5 篇文档（例如基础框架、产品分类、运作机制、风险收益、策略与评价），每个操作只负责一篇；已有对应文档则 update。
11. 用户只问一个窄概念或只选中一段求解释时，只更新当前文档或创建 1 篇，不要机械拆分。

只返回严格 JSON：
{"summary":"计划摘要","operations":[
  {"action":"update","scope_type":"domain","domain_key":"基金","document_id":1,"title":"可选的新标题","folder_name":"","goal":"具体修改目标","reason":"为什么改它"},
  {"action":"create","scope_type":"track","title":"新文档标题","folder_name":"面试准备","goal":"要形成的完整内容","reason":"为什么需要新建"}
]}
"""

KNOWLEDGE_SPACE_EDITOR_PROMPT = """你是 Caddie 的文档编辑 Agent。根据任务、操作目标和参考资料，产出一篇可以直接保存的完整 Markdown 文档。
保留原文中正确且仍有价值的内容；精确完成目标；层次清楚；面向求职准备时要能理解、能口述、能迁移；不要编造公司事实。
输入中的“完整岗位研究上下文”已经包含 Caddie 实际读取到的岗位、经历和相关准备资料。优先综合这些材料，不得声称用户尚未提供已经出现在上下文中的内容。
正文只能包含可长期复用的知识、事实、分析、方法或面试表达，不能包含你和用户的对话过程。
禁止写“好的”“明白”“收到”“上一轮”“这一轮”“根据你的要求”“下面我来”“我会为你”等回应式开场，也不要解释你如何完成任务。
公式使用标准 LaTeX：行内公式用 $...$，独立公式用 $$...$$，不要把公式放进代码块，也不要用单独的方括号包裹公式。
严格使用下列分隔符返回，不要 JSON，不要代码围栏：
SUMMARY: 一句话说明本篇改动
TITLE: 文档标题
===DOCUMENT===
完整 Markdown 正文
"""

KNOWLEDGE_SPACE_REVIEW_PROMPT = """你是变更审查 Agent。检查一组知识文档候选改动是否响应用户任务、是否重复、是否可能编造事实。
只返回严格 JSON：{"ready":true,"summary":"整体审查结论","warnings":["需要用户留意的点"]}
"""


def _parse_agent_document(raw: str, fallback_title: str) -> tuple[str, str, str]:
    marker = "===DOCUMENT==="
    if marker not in (raw or ""):
        raise ValueError("Agent 没有返回可应用的文档")
    head, document = raw.split(marker, 1)
    summary, title = "", fallback_title
    for line in head.splitlines():
        key, _, value = line.partition(":")
        if key.strip().upper() == "SUMMARY":
            summary = value.strip()
        elif key.strip().upper() == "TITLE" and value.strip():
            title = value.strip()
    document = _clean_agent_document(document)
    if not document:
        raise ValueError("Agent 返回的文档为空")
    return summary or "已形成候选修改", title or fallback_title, document


def _clean_agent_document(document: str) -> str:
    """Remove assistant chatter while preserving the durable document body."""
    text = (document or "").strip()
    lines = text.splitlines()
    chatter = re.compile(r"^(好的|明白|收到|可以|没问题|上一轮|这一轮|根据.{0,20}(要求|任务)|下面.{0,12}(整理|开始|给出)|我会|我将)")
    while lines and (not lines[0].strip() or chatter.match(lines[0].strip().lstrip("#>*- "))):
        lines.pop(0)
    text = "\n".join(lines).strip()
    # Models sometimes emit display LaTeX as bare [ ... ]; normalize the common case.
    text = re.sub(r"(?ms)^\[\s*(\\(?:frac|sum|text|Delta|approx|begin|mathbb)[\s\S]*?)\s*\]$", r"$$\n\1\n$$", text)
    return text


def _knowledge_space_catalog(track_id: int, items: list[dict], folders: list[dict]) -> str:
    folder_names = {x["id"]: x["name"] for x in folders}
    lines = []
    for item in items:
        # The planner may edit track-owned and inherited domain/company documents.
        # Global notes are searchable references, not an automatic job catalogue;
        # listing all of them caused unrelated topics to become false candidates.
        if item.get("scope_type") == "global":
            continue
        excerpt = re.sub(r"\s+", " ", item.get("content") or "").strip()[:650]
        lines.append(
            f"- id={item['id']} | scope_type={item.get('scope_type')} | "
            f"folder={folder_names.get(item.get('folder_id'),'未归档')} | title={item.get('title')} | excerpt={excerpt}"
        )
    return "\n".join(lines) or "（当前没有文档）"


def _run_knowledge_space_agent(tid: int, instruction: str, current_document_id=None, run_id=None,
                               model_provider=None) -> dict:
    track = db.get_job_track(tid)
    if not track:
        raise HTTPException(404, "岗位不存在")
    folders, _ = db.ensure_track_knowledge_folders(tid)
    items = db.list_knowledge_items(track_id=tid)
    if current_document_id and not any(x["id"] == current_document_id for x in items):
        raise HTTPException(400, "当前文档不属于这个岗位的知识空间")
    if run_id is None:
        run_id = db.create_knowledge_agent_run({
            "track_id": tid, "instruction": instruction,
            "current_document_id": current_document_id, "status": "planning",
        })
    try:
        db.create_knowledge_agent_event(run_id, "context", "读取岗位档案", "正在装配当前 JD、个人经历、同公司岗位和既有准备材料")
        research_context = caddie_context.build_job_research_context(tid, instruction)
        context_stats = research_context.get("stats") or {}
        db.create_knowledge_agent_event(
            run_id, "context", "完成上下文装配",
            f"读取 {context_stats.get('career_experiences', 0)} 段经历、"
            f"{context_stats.get('resume_documents', 0)} 份简历、"
            f"{context_stats.get('sibling_tracks', 0)} 个同公司岗位，"
            f"形成 {context_stats.get('references', 0)} 条可追溯引用",
        )
        try:
            search_results = retrieval.hybrid_search(instruction, track_id=tid, limit=8)
        except Exception:
            search_results = []
        adopted = "；".join(f"{x.get('title') or x.get('entity_type')}（{x.get('match_reason') or '相关'}）" for x in search_results[:5])
        db.create_knowledge_agent_event(run_id, "retrieval", "完成混合检索",
                                        f"采用 {len(search_results)} 条：{adopted}" if adopted else "没有采用低相关资料")
        references = "\n\n".join(
            f"【{x.get('title') or x.get('entity_type')}】\n{(x.get('snippet') or '')[:1200]}"
            for x in search_results
        )
        folder_list = "、".join(x["name"] for x in folders)
        planner_material = (
            f"【当前岗位】{track.get('company') or ''} · {track.get('role') or ''}\n"
            f"【目录】{folder_list}\n【当前打开文档】{current_document_id or '无'}\n"
            f"【用户任务】{instruction}\n\n【知识空间目录】\n"
            f"{_knowledge_space_catalog(tid, items, folders)}\n\n"
            f"【完整岗位研究上下文】\n{research_context.get('text','')[:30000]}\n\n"
            f"【本岗位混合检索结果】\n{references[:9000]}"
        )
        plan_raw = ai.chat([{"role": "user", "content": planner_material}],
                           system=KNOWLEDGE_SPACE_PLANNER_PROMPT, max_tokens=1600,
                           provider=model_provider)
        plan = ai.extract_json(plan_raw)
        operations = plan.get("operations") if isinstance(plan, dict) else None
        if not isinstance(operations, list) or not operations:
            raise ValueError("Agent 没有找到需要执行的文档操作")
        operations = operations[:5]
        db.create_knowledge_agent_event(run_id, "plan", "形成文档计划", f"规划 {len(operations)} 项候选操作")
        domain_candidates = db.knowledge_domain_keys(track)
        domain_key = domain_candidates[0] if domain_candidates else (track.get("role") or "行业知识")
        editable_items = {x["id"]: x for x in items if
                          (x.get("scope_type") == "track" and x.get("track_id") == tid) or
                          (x.get("scope_type") == "domain" and x.get("domain_key") in domain_candidates)}
        folder_by_name = {x["name"]: x for x in folders}
        edit_specs = []
        for op in operations:
            action = (op.get("action") or "").strip().lower()
            scope_type = (op.get("scope_type") or "track").strip().lower()
            if scope_type not in {"track", "domain"}:
                scope_type = "track"
            op_domain_key = (op.get("domain_key") or domain_key).strip() if scope_type == "domain" else domain_key
            if action not in {"update", "create"}:
                continue
            current = None
            if action == "update":
                try:
                    current = editable_items.get(int(op.get("document_id")))
                except (TypeError, ValueError):
                    current = None
                if not current:
                    continue
            folder = folder_by_name.get((op.get("folder_name") or "").strip()) if scope_type == "track" else None
            if scope_type == "track" and not folder:
                folder = next((x for x in folders if current and x["id"] == current.get("folder_id")), folders[0])
            fallback_title = (op.get("title") or (current or {}).get("title") or "未命名文档").strip()
            original = (current or {}).get("content") or ""
            supporting = references[:8000]
            editor_material = (
                f"【岗位】{track.get('company') or ''} · {track.get('role') or ''}\n"
                f"【知识作用域】{'行业/职能通用：不得写当前公司、JD、简历或面试话术' if scope_type == 'domain' else '当前岗位专属：可以使用公司、JD 和个人材料'}\n"
                f"【用户总任务】{instruction}\n【本篇目标】{op.get('goal') or ''}\n"
                f"【计划原因】{op.get('reason') or ''}\n【建议标题】{fallback_title}\n"
                f"【当前文档】\n{original[:18000] if original else '（新建文档）'}\n\n"
                f"【完整岗位研究上下文】\n{research_context.get('text','')[:28000]}\n\n"
                f"【本岗位相关资料，仅作事实与结构参考】\n{supporting}"
            )
            edit_specs.append({"op": op, "action": action, "current": current,
                               "folder": folder, "fallback_title": fallback_title,
                               "original": original, "material": editor_material,
                               "scope_type": scope_type, "domain_key": op_domain_key})
        if not edit_specs:
            raise ValueError("计划中没有可安全执行的操作")

        # Convert duplicate creates to updates when a same-title track document already exists.
        title_index = {re.sub(r"\s+", "", (x.get("title") or "")).lower(): x for x in editable_items.values()}
        seen_create_titles = set()
        normalized_specs = []
        for spec in edit_specs:
            key = re.sub(r"\s+", "", spec["fallback_title"]).lower()
            existing = title_index.get(key)
            if spec["action"] == "create" and existing:
                spec.update({"action": "update", "current": existing,
                             "original": existing.get("content") or ""})
                spec["material"] = spec["material"].replace("【当前文档】\n（新建文档）", f"【当前文档】\n{spec['original'][:18000]}")
            elif spec["action"] == "create" and key in seen_create_titles:
                continue
            if spec["action"] == "create":
                seen_create_titles.add(key)
            normalized_specs.append(spec)
        edit_specs = normalized_specs
        db.update_knowledge_agent_run(run_id, status="editing")
        db.create_knowledge_agent_event(run_id, "edit", "逐篇整理知识", f"正在生成 {len(edit_specs)} 篇候选文档", "running")

        def edit_one(spec):
            edited_raw = ai.chat([{"role": "user", "content": spec["material"]}],
                                 system=KNOWLEDGE_SPACE_EDITOR_PROMPT, max_tokens=4200,
                                 provider=model_provider)
            return _parse_agent_document(edited_raw, spec["fallback_title"])

        # Independent document edits can run together; cap concurrency to avoid provider throttling.
        with ThreadPoolExecutor(max_workers=min(3, len(edit_specs))) as pool:
            edited_results = list(pool.map(edit_one, edit_specs))
        db.create_knowledge_agent_event(run_id, "edit", "完成候选文档", f"已生成 {len(edited_results)} 篇候选内容")

        change_summaries = []
        for spec, edited in zip(edit_specs, edited_results):
            op, action, current = spec["op"], spec["action"], spec["current"]
            folder, original = spec["folder"], spec["original"]
            edit_summary, proposed_title, proposed_content = edited
            old_for_diff = original
            new_for_diff = proposed_content
            if current and proposed_title != current.get("title"):
                old_for_diff = f"# {current.get('title')}\n\n{old_for_diff}"
                new_for_diff = f"# {proposed_title}\n\n{new_for_diff}"
            diff = "\n".join(difflib.unified_diff(
                old_for_diff.splitlines(), new_for_diff.splitlines(),
                fromfile="当前文档" if current else "空白文档",
                tofile="候选文档", lineterm="",
            ))
            db.create_knowledge_agent_change({
                "run_id": run_id, "action_type": action,
                "document_id": current.get("id") if current else None,
                "folder_id": folder["id"] if folder else None, "proposed_title": proposed_title,
                "proposed_content": proposed_content, "diff": diff[:24000],
                "reason": op.get("reason") or edit_summary,
                "metadata_json": json.dumps({"summary": edit_summary, "goal": op.get("goal") or "",
                                             "scope_type": spec["scope_type"],
                                             "domain_key": spec["domain_key"]}, ensure_ascii=False),
            })
            change_summaries.append(
                f"- {action}《{proposed_title}》：{edit_summary}\n{diff[:3500]}"
            )
        # Final checks are deterministic to avoid another slow model round-trip.
        generated = db.get_knowledge_agent_run(run_id).get("changes") or []
        titles = [x.get("proposed_title") or "" for x in generated]
        warnings = []
        if len(set(titles)) != len(titles):
            warnings.append("候选改动中有同名文档，请确认是否需要合并")
        for change in generated:
            if len((change.get("proposed_content") or "").strip()) < 80:
                warnings.append(f"《{change.get('proposed_title') or '未命名文档'}》内容较短")
            if change.get("action_type") == "update" and not (change.get("diff") or "").strip():
                warnings.append(f"《{change.get('proposed_title') or '未命名文档'}》与原文没有明显差异")
        review = {
            "ready": not warnings,
            "summary": plan.get("summary") or f"已形成 {len(generated)} 项候选改动，并完成本地一致性检查。",
            "warnings": warnings,
        }
        summary = review["summary"]
        db.update_knowledge_agent_run(run_id, status="ready", summary=summary,
                                      review_json=json.dumps(review, ensure_ascii=False))
        db.create_knowledge_agent_event(run_id, "review", "完成一致性检查", summary)
        return db.get_knowledge_agent_run(run_id)
    except ai.AIError as exc:
        db.update_knowledge_agent_run(run_id, status="failed", summary=str(exc)[:300])
        raise HTTPException(400, str(exc))
    except Exception as exc:
        db.update_knowledge_agent_run(run_id, status="failed", summary=str(exc)[:300])
        raise HTTPException(500, f"知识空间 Agent 执行失败：{str(exc)[:240]}")


def _run_knowledge_space_agent_background(tid, instruction, current_document_id, run_id):
    try:
        _run_knowledge_space_agent(tid, instruction, current_document_id, run_id)
    except Exception:
        # The run status and reason have already been persisted for polling clients.
        return


@app.post("/api/job-tracks/{tid}/knowledge-agent/runs")
def create_knowledge_space_agent_run(tid: int, body: KnowledgeSpaceAgentIn, background_tasks: BackgroundTasks):
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(400, "请告诉 Agent 要完成什么")
    if not db.get_job_track(tid):
        raise HTTPException(404, "岗位不存在")
    run_id = db.create_knowledge_agent_run({
        "track_id": tid, "instruction": instruction,
        "current_document_id": body.current_document_id, "status": "planning",
    })
    db.create_knowledge_agent_event(run_id, "queued", "任务已创建", "等待 Agent 开始处理")
    background_tasks.add_task(_run_knowledge_space_agent_background, tid, instruction,
                              body.current_document_id, run_id)
    return db.get_knowledge_agent_run(run_id)


@app.get("/api/knowledge-agent/runs/{rid}")
def get_knowledge_space_agent_run(rid: int):
    run = db.get_knowledge_agent_run(rid)
    if not run:
        raise HTTPException(404, "Agent 运行不存在")
    return run


@app.post("/api/knowledge-agent/runs/{rid}/apply")
def apply_knowledge_space_agent_run(rid: int, body: KnowledgeSpaceAgentApplyIn):
    run = db.get_knowledge_agent_run(rid)
    if not run:
        raise HTTPException(404, "Agent 运行不存在")
    valid = {x["id"] for x in run.get("changes") or [] if x.get("status") == "pending"}
    selected = [x for x in body.change_ids if x in valid]
    if not selected:
        raise HTTPException(400, "请至少选择一项改动")
    try:
        results = db.apply_knowledge_agent_run(rid, selected)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    track = db.get_job_track(run["track_id"]) or {}
    _commit(f"Agent 整理岗位知识：{track.get('company') or ''} · {track.get('role') or ''}（{len(results)} 项）")
    return {"ok": True, "results": results, "run": db.get_knowledge_agent_run(rid)}


@app.post("/api/knowledge-agent/runs/{rid}/cancel")
def cancel_knowledge_space_agent_run(rid: int):
    run = db.get_knowledge_agent_run(rid)
    if not run:
        raise HTTPException(404, "Agent 运行不存在")
    if run.get("status") in {"applied", "cancelled"}:
        return {"ok": True, "run": run}
    for change in run.get("changes") or []:
        if change.get("status") == "pending":
            db.update_knowledge_agent_change(change["id"], "ignored")
    db.update_knowledge_agent_run(rid, status="cancelled")
    return {"ok": True, "run": db.get_knowledge_agent_run(rid)}


@app.get("/api/knowledge-items/{kid}/messages")
def knowledge_messages(kid: int):
    if not db.get_knowledge_item(kid):
        raise HTTPException(404, "知识不存在")
    return db.get_chat_history(f"knowledge-{kid}", limit=100)


@app.delete("/api/knowledge-items/{kid}/messages")
def clear_knowledge_messages(kid: int):
    db.clear_chat(f"knowledge-{kid}")
    return {"ok": True}


@app.post("/api/knowledge-items/{kid}/explain")
def explain_knowledge_selection(kid: int, body: dict):
    """Explain a selected passage without producing or applying document changes."""
    item = db.get_knowledge_item(kid)
    if not item:
        raise HTTPException(404, "知识不存在")
    selection = (body.get("selection") or "").strip()
    question = (body.get("question") or "请把这段内容讲懂").strip()
    if not selection:
        raise HTTPException(400, "请先选择一段内容")
    prompt = (
        f"【文档】{item.get('title') or '未命名知识'}\n"
        f"【用户选中的内容】\n{selection[:5000]}\n\n"
        f"【用户问题】\n{question[:1000]}"
    )
    system = """你是耐心、严谨的知识教练。当前任务只是解释，不是修改文档，也不是面试辅导。
先用直觉和背景讲清楚，再解释原理、必要的公式和一个具体例子；指出容易混淆的概念。
不要加入面试话术、公司包装、岗位适配或对话寒暄。若信息不足，明确说哪里需要补充。"""
    try:
        reply = ai.chat([{"role": "user", "content": prompt}], system=system,
                        max_tokens=1600, profile_key="writing")
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))
    return {"reply": reply, "mode": "explain_only"}


@app.post("/api/knowledge-items/{kid}/agent")
def knowledge_agent(kid: int, body: dict):
    import difflib
    item = db.get_knowledge_item(kid)
    if not item:
        raise HTTPException(404, "知识不存在")
    instruction = (body.get("message") or "").strip()
    if not instruction:
        raise HTTPException(400, "请告诉 Agent 想怎样完善这篇知识")
    sid = f"knowledge-{kid}"
    history = db.get_chat_history(sid, limit=10)
    history_text = "\n".join(
        f"{'用户' if x['role']=='user' else 'Agent'}：{x['content']}" for x in history
    )
    scope = _enrich_knowledge_item(item).get("scope_label") or item.get("scope_type")
    material = (f"【知识标题】{item.get('title') or ''}\n【复用范围】{scope}\n"
                f"【主题】{item.get('topic') or ''}\n\n【当前文档】\n{item.get('content') or ''}\n\n"
                f"【此前协作记录】\n{history_text[-5000:]}\n\n【本次指令】\n{instruction}")
    try:
        raw = ai.chat([{"role": "user", "content": material}],
                      system=KNOWLEDGE_AGENT_PROMPT, max_tokens=3500,
                      profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:200]}")
    marker = "===DOCUMENT==="
    if marker not in raw:
        raise HTTPException(500, "Agent 没有返回可应用的文档，请重试")
    head, document = raw.split(marker, 1)
    summary = head.strip()
    if summary.upper().startswith("SUMMARY:"):
        summary = summary.split(":", 1)[1].strip()
    document = document.strip()
    if not document:
        raise HTTPException(500, "Agent 返回的文档为空，请重试")
    db.save_message(sid, "user", instruction)
    db.save_message(sid, "assistant", summary)
    diff = "\n".join(difflib.unified_diff(
        (item.get("content") or "").splitlines(), document.splitlines(),
        fromfile="当前文档", tofile="候选修改", lineterm="",
    ))
    return {
        "reply": summary or "已整理一版候选修改",
        "proposed_content": document,
        "diff": diff[:16000],
    }


@app.put("/api/job-tracks/{tid}")
def edit_job_track(tid: int, body: JobTrackIn):
    db.update_job_track(tid, body.dict())
    _workspace_event("track_updated", "job_track", tid, f"用户更新求职线：{body.company or ''} · {body.role or ''}", "track", tid)
    _commit(f"编辑求职线：{body.company or ''} · {body.role or ''}")
    return {"ok": True}


@app.post("/api/job-tracks/{tid}/sync-applications")
def sync_job_track_applications(tid: int):
    t = db.sync_track_from_applications(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    _commit(f"同步投递信息到求职线：{t.get('company') or ''} · {t.get('role') or ''}")
    return {"track": t}


@app.post("/api/job-tracks/{tid}/application")
def ensure_job_track_application(tid: int, body: dict):
    application, created = db.ensure_track_application(tid, body)
    if not application:
        raise HTTPException(404, "求职线不存在")
    if created:
        _commit(f"从求职线建立投递：{application.get('company') or ''} · {application.get('role') or ''}")
    return {"application": application, "created": created}


@app.post("/api/job-tracks/{tid}/classify-company")
def classify_job_track_company(tid: int):
    t = db.classify_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    _commit(f"自动归类公司：{t.get('company') or ''}")
    return {"track": t}


@app.post("/api/job-tracks/classify-companies")
def classify_all_job_track_companies():
    n = db.classify_all_job_tracks()
    _commit(f"自动归类 {n} 条求职线公司")
    return {"ok": True, "count": n}


@app.delete("/api/job-tracks/{tid}")
def del_job_track(tid: int):
    t = db.get_job_track(tid) or {}
    if not t or not db.delete_job_track(tid):
        raise HTTPException(404, "求职线不存在")
    _commit(f"删除求职线：{t.get('company','')} · {t.get('role','')}")
    return {"ok": True}


# ─── 求职线：差距清单 CRUD ────────────────────────────────────────────────────

@app.get("/api/job-tracks/{tid}/gaps")
def list_gaps(tid: int):
    return {"items": db.list_track_gaps(tid)}


@app.post("/api/job-tracks/{tid}/gaps")
def add_gap(tid: int, body: TrackGapIn):
    d = body.dict(); d["track_id"] = tid
    gid = db.create_track_gap(d)
    _commit("新增差距项")
    return {"id": gid}


@app.put("/api/gaps/{gid}")
def edit_gap(gid: int, body: TrackGapIn):
    db.update_track_gap(
        gid,
        {k: v for k, v in body.dict(exclude_unset=True).items() if v is not None},
    )
    return {"ok": True}


@app.delete("/api/gaps/{gid}")
def del_gap(gid: int):
    db.delete_track_gap(gid)
    return {"ok": True}


@app.post("/api/gaps/{gid}/score-exclusion")
def set_gap_score_exclusion(gid: int, body: dict):
    gap = db.get_track_gap(gid)
    if not gap:
        raise HTTPException(404, "差距项不存在")
    excluded = bool(body.get("excluded"))
    reason = (body.get("reason") or "").strip()
    if excluded and not reason:
        reason = _non_scoring_requirement_reason(gap.get("requirement")) or "用户确认这不是能力匹配要求"
    db.update_track_gap(gid, {
        "excluded_from_score": 1 if excluded else 0,
        "exclusion_reason": reason if excluded else None,
    })
    learned = False
    if excluded and body.get("remember", True):
        requirement = (gap.get("requirement") or "").strip()
        directive = (
            f"岗位匹配评分中不要计入这类非能力条件：{requirement[:120]}。"
            "它可以保留为岗位事实或提醒，但不能降低证据匹配分。"
        )
        existing = db.list_feedback_notes(scope="global", status="active")
        if not any((x.get("directive") or "") == directive for x in existing):
            db.create_feedback_note({
                "scope": "global",
                "note_type": "feedback",
                "category": "match_scoring",
                "polarity": "avoid",
                "strength": "hard",
                "directive": directive,
                "content": reason,
                "original_text": requirement,
                "status": "active",
            })
            learned = True
    _commit("调整岗位要求评分范围")
    return {
        "ok": True,
        "excluded_from_score": excluded,
        "reason": reason if excluded else None,
        "learned": learned,
    }


@app.post("/api/gaps/{gid}/knowledge")
def create_gap_knowledge(gid: int):
    gap = db.get_track_gap(gid)
    if not gap:
        raise HTTPException(404, "差距项不存在")
    track = db.get_job_track(gap.get("track_id"))
    if not track:
        raise HTTPException(404, "岗位不存在")
    folders, _ = db.ensure_track_knowledge_folders(track["id"])
    target_name = "面试准备" if gap.get("plan_type") == "pitch" else "专业知识"
    folder = next((item for item in folders if item.get("name") == target_name), None)
    if not folder:
        raise HTTPException(500, "岗位准备目录不完整")
    title = (gap.get("requirement") or "待补知识").strip()[:80]
    note = (gap.get("note") or "").strip()
    content = (
        f"# {title}\n\n"
        f"> 来源：{track.get('company') or ''} · {track.get('role') or track.get('target') or ''} 的差距诊断\n\n"
        "## 为什么要准备\n\n"
        f"{note or '这项能力在岗位要求中尚未充分证明，需要补充知识、案例或可口述的表达。'}\n\n"
        "## 核心知识\n\n从这里开始整理概念、框架、公式或案例。\n\n"
        "## 面试怎么回答\n\n把知识转成一段可以直接口述的回答。\n\n"
        "## 可能追问\n\n- \n"
    )
    kid = db.create_knowledge_item({
        "title": title, "content": content, "scope_type": "track",
        "track_id": track["id"], "folder_id": folder["id"],
        "topic": gap.get("dimension") or "", "mastery": "new", "status": "active",
        "source_type": "gap", "source_ref_id": gid,
    })
    db.update_track_gap(gid, {"status": "doing", "plan_ref_type": "knowledge", "plan_ref_id": kid})
    _commit(f"从差距建立知识文档：{title}")
    return {"item": _enrich_knowledge_item(db.get_knowledge_item(kid)), "folder": folder}


# ─── 求职线：AI 岗位画像 + 差距诊断 ───────────────────────────────────────────

PERSONA_PROMPT = """你是资深招聘官。读这段 JD，提炼这个岗位真正想要什么样的人（岗位画像）。
输出 4-6 条，每条一句话，并严格使用以下标签之一开头：
[核心任务]
[硬性门槛]
[关键能力]
[工作方式]
[加分偏好]
同类标签可以重复，不存在的类别不要硬凑。用具体、可核验的表达，不要空泛形容词，不要表情符号，不要 markdown 符号。"""

GAP_DIAGNOSE_PROMPT = """你是求职教练。对比【岗位要求】和【候选人现有背景】，诊断差距，输出差距清单。

【岗位要求 / JD】
%s

【候选人现有背景（经历/项目/技能）】
%s

为每个关键要求判断候选人的匹配情况，输出 JSON（不要 markdown 围栏）：
{"gaps":[
  {"dimension":"硬技能/项目经历/领域知识/软素质","requirement":"JD里的具体要求","my_status":"have/partial/missing","severity":"blocker/fixable/bypass","plan_type":"knowledge/portfolio/pitch/none","note":"一句话建议怎么补或怎么讲"}
]}
- have=已具备 partial=部分 missing=缺；blocker=硬伤 fixable=可补 bypass=可绕过
- plan_type：knowledge=补知识 portfolio=补作品 pitch=补话术(把现有经历往JD靠) none=无需
- 只评估候选人能够用经历、能力或知识证明的要求。
- base 地/工作地点、到岗或截止日期、实习时长、薪资福利、投递方式、团队氛围、mentor 风格和决策权等非能力条件，不进入匹配评分。
- 控制在 8 条以内，最关键的在前。"""


@app.post("/api/job-tracks/{tid}/persona")
def gen_persona(tid: int):
    t = db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    if not (t.get("jd") or "").strip():
        raise HTTPException(400, "先填写这个目标的 JD")
    out = ai.chat([{"role": "user", "content": t["jd"][:6000]}], system=PERSONA_PROMPT,
                  max_tokens=600, profile_key="research")
    db.update_job_track(tid, {**t, "persona": out.strip()})
    _commit("生成岗位画像")
    return {"persona": out.strip()}


@app.post("/api/job-tracks/{tid}/diagnose")
def diagnose_gaps(tid: int):
    t = db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职线不存在")
    if not (t.get("jd") or "").strip():
        raise HTTPException(400, "先填写这个目标的 JD")
    background = _full_career_context(6000)
    diagnosis = _fallback_gap_diagnosis(t["jd"], background)
    existing = db.list_track_gaps(tid)

    def requirement_key(value):
        return re.sub(r"[\W_]+", "", value or "").lower()

    by_requirement = {
        requirement_key(item.get("requirement")): item
        for item in existing
        if requirement_key(item.get("requirement"))
    }
    created = 0
    updated = 0
    for item in diagnosis.get("gaps") or []:
        key = requirement_key(item.get("requirement"))
        if not key:
            continue
        current = by_requirement.get(key)
        non_scoring_reason = _non_scoring_requirement_reason(item.get("requirement"))
        if current:
            patch = {
                "dimension": item.get("dimension"),
                "my_status": item.get("my_status"),
                "severity": item.get("severity"),
                "plan_type": item.get("plan_type"),
                "note": item.get("note"),
            }
            if non_scoring_reason:
                patch.update({
                    "excluded_from_score": 1,
                    "exclusion_reason": non_scoring_reason,
                })
            db.update_track_gap(current["id"], patch)
            updated += 1
        else:
            item["track_id"] = tid
            if non_scoring_reason:
                item["excluded_from_score"] = True
                item["exclusion_reason"] = non_scoring_reason
            db.create_track_gap(item)
            created += 1
    if created or updated:
        _commit(f"更新岗位匹配：新增 {created} 条，更新 {updated} 条")
    return {
        "gaps": db.list_track_gaps(tid),
        "created": created,
        "updated": updated,
    }


@app.post("/api/job-tracks/{tid}/gaps/bulk")
def add_gaps_bulk(tid: int, body: dict):
    items = body.get("items") or []
    n = 0
    for g in items:
        g["track_id"] = tid
        db.create_track_gap(g)
        n += 1
    _commit(f"采纳 {n} 条差距诊断")
    return {"ok": True, "count": n}


# ─── 求职目标工作台：针对这个岗位的 AI 对话 + agent 模式 ────────────────────────

TRACK_CHAT_BASE = """你是 Caddie，位于一个具体岗位的信息工作台。你能读取岗位档案，但必须先服从用户本轮目标，不能把所有问题都扭成面试准备。
你掌握：岗位 JD 与画像、用户经历、已有知识和资料。只采用与当前问题直接相关的上下文；检索到不等于相关，不要强行引用。
知识目录与正文检索必须严格区分：
1. “当前岗位完整知识目录”代表真实存在的文档。用户点名目录中的文档时，不得声称不存在，也不得要求重新上传。
2. 本轮没有检索到正文，只能表述为“正文尚未加载/本轮未命中”，绝不能推断为“没有该文档”。
3. 用户指出资料位于当前岗位、准备知识或某个文件夹时，先检查完整目录；目录已列出目标文档时，应直接使用本轮命中的正文，或明确说明需要继续读取，不能把查找责任推回用户。
4. 用户一次提到多份资料时，逐份确认目录命中情况，不得只处理第一份。
原则：用中文，准确、具体、循序渐进；用户要学知识时就把知识讲懂，不主动插入面试话术、岗位包装或“面试官会问”。"""

TRACK_CHAT_MODES = {
    "learn": "\n本轮是【学习知识】：目标是让用户真正理解概念、原理、公式、例子和知识之间的关系。先讲清楚，再逐层深入；除非用户明确要求，不讨论面试。通用知识应标注为行业/职能通用，而非岗位专属。",
    "general": "\n本轮是【岗位适配】：回答当前岗位相关问题，区分行业通用信息与公司/岗位特有信息。",
    "mock": "\n本轮是【模拟面试】：你扮演这个岗位的面试官，基于 JD 和已知差距提问，一次问一两个，用户回答后简短点评再追问，像真面试。",
    "pitch": "\n本轮是【打磨话术】：帮用户把某段真实经历组织成适配这个 JD 的讲法，主动用 STAR、突出与岗位的相关性，并标出需要用户补充事实的地方。",
}


def _track_context(t, gaps):
    parts = [
        f"【岗位】{t.get('company','')} · {t.get('role') or t.get('target','')}",
        f"【JD】\n{(t.get('jd') or '（未填写）')[:3500]}",
    ]
    if t.get("persona"):
        parts.append(f"【岗位画像】\n{t['persona']}")
    if gaps:
        gl = []
        sl = {"have": "已具备", "partial": "部分", "missing": "缺"}
        for g in gaps:
            gl.append(f"- [{sl.get(g.get('my_status'),'?')}] {g.get('requirement','')}"
                      + (f"（{g.get('note')}）" if g.get("note") else ""))
        parts.append("【已诊断差距】\n" + "\n".join(gl))
    knowledge = db.list_knowledge_items(track_id=t.get("id"))
    if knowledge:
        scope_names = {"global": "个人", "domain": "行业/职能", "company": "公司", "track": "岗位"}
        lines = []
        for item in knowledge[:30]:
            lines.append(
                f"- [{scope_names.get(item.get('scope_type'),'知识')}] {item.get('title') or ''}："
                f"{(item.get('content') or '')[:700]}"
            )
        parts.append("【本岗位可用知识】\n" + "\n".join(lines))
    fb = _focus_block()
    if fb:
        parts.append(fb)
    parts.append("【用户完整背景】\n" + _full_career_context(5000))
    return "\n\n".join(parts)


@app.get("/api/job-tracks/{tid}/messages")
def track_messages(tid: int, conversation_id: Optional[str] = None):
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "", conversation_id or "")[:64]
    sid = f"track-{tid}-{suffix}" if suffix else f"track-{tid}"
    return db.get_chat_history(sid, limit=200)


def _assistant_sources(context_data: dict):
    labels = {
        "job_track": "岗位档案", "track_gap": "差距项", "knowledge_item": "知识",
        "source": "原始资料", "asset": "生成产物", "project": "项目",
    }
    seen, items = set(), []
    for ref in context_data.get("refs") or []:
        key = (ref.get("type"), ref.get("id"))
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "type": ref.get("type"), "id": ref.get("id"),
            "label": labels.get(ref.get("type"), "资料"),
            "title": ref.get("title") or labels.get(ref.get("type"), "资料"),
            "reason": ref.get("match_reason") or ("当前岗位档案" if ref.get("type") == "job_track" else "上下文关联"),
        })
    return items[:24]


@app.get("/api/job-tracks/{tid}/assistant-context")
def track_assistant_context(tid: int):
    if not db.get_job_track(tid):
        raise HTTPException(404, "求职目标不存在")
    data = caddie_context.build_context(track_id=tid, intent="track_assistant_preview")
    sources = _assistant_sources(data)
    counts = {}
    for item in sources:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    return {"sources": sources, "counts": counts, "feedback_count": len(data.get("feedback") or [])}


@app.get("/api/retrieval/search")
def search_retrieval(q: str, track_id: Optional[int] = None, limit: int = 8):
    try:
        items = retrieval.hybrid_search(q, track_id=track_id, limit=max(1, min(limit, 20)))
    except Exception as exc:
        raise HTTPException(500, f"混合检索失败：{str(exc)[:200]}")
    return {"items": items, "query": q, "mode": "fts5+bge-small-zh-v1.5"}


@app.post("/api/job-tracks/{tid}/chat")
def track_chat(tid: int, body: dict):
    t = db.get_job_track(tid)
    if not t:
        raise HTTPException(404, "求职目标不存在")
    gaps = db.list_track_gaps(tid)
    conversation_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("conversation_id") or ""))[:64]
    sid = f"track-{tid}-{conversation_id}" if conversation_id else f"track-{tid}"
    history = db.get_chat_history(sid, limit=16)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    current_message = body.get("message", "")
    messages.append({"role": "user", "content": current_message})
    mode = body.get("mode", "general")
    # Retrieval needs conversational references, not only the latest sentence.
    # "把这三个放进去" is meaningless without the document names from the
    # preceding user turns. Keep the window user-only to avoid searching the
    # assistant's speculative prose as if it were evidence.
    recent_user_intent = [
        item.get("content") or ""
        for item in history
        if item.get("role") == "user"
    ][-4:]
    retrieval_query = "\n".join([*recent_user_intent, current_message]).strip()
    context_data = caddie_context.build_context(
        track_id=tid, intent=f"track_chat:{mode}", query=retrieval_query,
    )
    system = TRACK_CHAT_BASE + TRACK_CHAT_MODES.get(mode, TRACK_CHAT_MODES["general"]) + "\n\n" + context_data["text"]
    try:
        reply = ai.chat(messages, system=system, max_tokens=1800,
                        profile_key="deep_reasoning")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:200]}")
    user_message_id = db.save_message(sid, "user", body.get("message", ""))
    assistant_message_id = db.save_message(sid, "assistant", reply)
    return {
        "reply": reply, "sources": _assistant_sources(context_data),
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    }


@app.delete("/api/job-tracks/{tid}/messages")
def clear_track_chat(tid: int, conversation_id: Optional[str] = None):
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "", conversation_id or "")[:64]
    db.clear_chat(f"track-{tid}-{suffix}" if suffix else f"track-{tid}")
    return {"ok": True}


@app.delete("/api/job-tracks/{tid}/messages/from/{message_id}")
def truncate_track_chat(tid: int, message_id: int, conversation_id: Optional[str] = None):
    if not db.get_job_track(tid):
        raise HTTPException(404, "求职目标不存在")
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "", conversation_id or "")[:64]
    sid = f"track-{tid}-{suffix}" if suffix else f"track-{tid}"
    if not db.truncate_chat(sid, message_id):
        raise HTTPException(404, "消息不存在或不属于当前对话")
    return {"ok": True}


ASSET_TYPE_LABEL = {
    "project_pitch": "项目话术", "self_intro": "自我介绍", "knowledge": "知识卡",
    "resume": "定制简历", "gap_analysis": "JD 对比", "review": "复盘",
    "worklog_digest": "日记提炼", "other": "其他",
}
ASSET_STATUS_LABEL = {"draft": "草稿", "final": "定稿", "stale": "待更新"}


def _enrich_asset(a: dict) -> dict:
    """给资产补上可读的类型标签、关联项目/求职线名字。"""
    a["type_label"] = ASSET_TYPE_LABEL.get(a.get("asset_type"), a.get("asset_type"))
    a["status_label"] = ASSET_STATUS_LABEL.get(a.get("status"), a.get("status"))
    a["project_name"] = None
    a["track_name"] = None
    if a.get("project_id"):
        p = db.get_project(a["project_id"])
        if p:
            a["project_name"] = p.get("name")
    if a.get("track_id"):
        t = db.get_job_track(a["track_id"])
        if t:
            a["track_name"] = f"{t.get('company') or ''} {t.get('role') or t.get('target') or ''}".strip()
    return a


@app.get("/api/assets")
def list_assets(asset_type: Optional[str] = None, track_id: Optional[int] = None, project_id: Optional[int] = None):
    items = [_enrich_asset(a) for a in db.list_assets(asset_type=asset_type, track_id=track_id, project_id=project_id)]
    return {"items": items, "types": ASSET_TYPE_LABEL}


@app.get("/api/assets/{aid}")
def get_asset(aid: int):
    asset = db.get_asset(aid)
    if not asset:
        raise HTTPException(404, "资产不存在")
    if asset.get("provenance_json"):
        try:
            asset["provenance"] = json.loads(asset["provenance_json"])
        except Exception:
            asset["provenance"] = None
    if asset.get("derived_from_json"):
        try:
            asset["derived_from"] = json.loads(asset["derived_from_json"])
        except Exception:
            asset["derived_from"] = None
    return _enrich_asset(asset)


@app.post("/api/assets")
def add_asset(body: AssetIn):
    aid = db.create_asset(body.dict())
    _commit(f"新增资产：{body.title or body.asset_type}")
    telemetry.log_event(
        "asset_saved",
        {"asset_type": body.asset_type or "other", "was_edited": False},
        entity_type="asset",
        entity_id=aid,
    )
    return {"id": aid}


class AssetUpdateIn(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    status: Optional[str] = None


class FollowupIn(BaseModel):
    question: str
    answer: Optional[str] = None
    status: Optional[str] = "todo"
    category: Optional[str] = None
    asked_count: Optional[int] = 0
    origin: Optional[str] = "manual"
    source_track_id: Optional[int] = None
    kind: Optional[str] = None


class FollowupUpdateIn(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    asked_count: Optional[int] = None
    origin: Optional[str] = None
    source_track_id: Optional[int] = None
    kind: Optional[str] = None


@app.put("/api/assets/{aid}")
def edit_asset(aid: int, body: AssetUpdateIn):
    cur = db.get_asset(aid)
    if not cur:
        raise HTTPException(404, "资产不存在")
    merged = {
        "title": body.title if body.title is not None else cur.get("title"),
        "body": body.body if body.body is not None else cur.get("body"),
        "status": body.status if body.status is not None else cur.get("status"),
    }
    db.update_asset(aid, merged)
    _commit(f"更新资产：{merged['title'] or aid} → {ASSET_STATUS_LABEL.get(merged['status'], merged['status'])}")
    return {"ok": True}


@app.delete("/api/assets/{aid}")
def remove_asset(aid: int):
    db.delete_asset(aid)
    _commit("删除资产")
    return {"ok": True}


@app.get("/api/projects/{pid}/followups")
def list_project_followups(pid: int, status: Optional[str] = None):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return {"items": db.list_followups(project_id=pid, status=status)}


@app.post("/api/projects/{pid}/followups")
def add_project_followup(pid: int, body: FollowupIn):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not (body.question or "").strip():
        raise HTTPException(400, "追问不能为空")
    data = body.dict()
    if (data.get("category") or "").strip():
        data["category_source"] = "user"
    fid = db.create_followup(pid, data)
    _commit(f"新增项目追问：{p.get('name')}")
    return {"id": fid}


@app.put("/api/followups/{fid}")
def edit_followup(fid: int, body: FollowupUpdateIn):
    cur = db.get_followup(fid)
    if not cur:
        raise HTTPException(404, "追问不存在")
    patch = body.dict(exclude_unset=True)
    old_cat = cur.get("category")
    if "category" in patch and patch.get("category") != old_cat:
        patch["category_source"] = "user"
        patch["previous_category"] = old_cat
        q = cur.get("question") or ""
        new_cat = patch.get("category") or "未分类"
        db.create_feedback_note({
            "scope": "global",
            "category": "taxonomy",
            "polarity": "do",
            "strength": "soft",
            "directive": f"把形如「{q[:60]}」的追问归类到「{new_cat}」",
            "content": f"追问分类学习：{old_cat or '未分类'} → {new_cat}",
            "original_text": q,
        })
    db.update_followup(fid, patch)
    _commit("更新项目追问")
    return {
        "ok": True,
        "learned": "category" in patch and patch.get("category") != old_cat,
        "previous_category": old_cat,
        "category": patch.get("category", old_cat),
    }


@app.delete("/api/followups/{fid}")
def remove_followup(fid: int):
    cur = db.get_followup(fid)
    if not cur:
        raise HTTPException(404, "追问不存在")
    db.delete_followup(fid)
    _commit("删除项目追问")
    return {"ok": True}


FOLLOWUP_CATEGORIES = [
    "项目贡献", "数据方法", "业务理解", "技术实现",
    "结果量化", "反事实追问", "风险不足", "协作推进", "数据缺口", "其他",
]


def _taxonomy_feedback():
    taxonomy = db.list_feedback_notes(scope="global", status="active")
    return [x for x in taxonomy if x.get("category") == "taxonomy"]


def _classify_followup_items(items):
    if not items:
        return {}
    taxonomy = _taxonomy_feedback()
    prompt = f"""请给下面这些项目面试追问分类。分类名要短、稳定、可复用，例如：项目贡献 / 数据方法 / 业务理解 / 技术实现 / 结果量化 / 反事实追问 / 风险不足。

用户已有分类偏好：
{chr(10).join('- '+(x.get('directive') or x.get('content') or '') for x in taxonomy[:20]) or '（无）'}

优先从这些稳定分类中选；确实不合适时才创建新的短分类：
{' / '.join(FOLLOWUP_CATEGORIES)}

追问列表：
{json.dumps([{'id': x['id'], 'question': x['question']} for x in items], ensure_ascii=False)}

严格返回 JSON：
{{"items":[{{"id":1,"category":"项目贡献"}}]}}"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=800,
                      profile_key="fast")
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"归类失败：{str(e)[:160]}")
    idset = {x["id"] for x in items}
    result = {}
    for x in data.get("items", []) if isinstance(data, dict) else []:
        category = str(x.get("category") or "").strip()[:24]
        if x.get("id") in idset and category:
            result[x["id"]] = category
    return result


def _knowledge_write_receipt(item: dict, action_type: str) -> dict:
    scope = item.get("scope_type") or "global"
    track = db.get_job_track(item.get("track_id")) if item.get("track_id") else None
    folder = db.get_knowledge_folder(item.get("folder_id")) if item.get("folder_id") else None
    if track:
        path = " / ".join(filter(None, [
            track.get("company"), track.get("role") or track.get("target"),
            "准备知识", (folder or {}).get("name"), item.get("title"),
        ]))
        open_target = {
            "view": "track", "track_id": track["id"], "tab": "knowledge",
            "knowledge_item_id": item["id"],
        }
    else:
        scope_label = {
            "global": "个人通用", "domain": "行业 / 职能", "company": "公司共享",
        }.get(scope, scope)
        path = " / ".join(filter(None, ["知识空间", scope_label, item.get("title")]))
        open_target = {
            "view": "knowledge", "knowledge_item_id": item["id"],
        }
    return {
        "knowledge_item_id": item["id"], "scope_type": scope,
        "track_id": item.get("track_id"), "folder_id": item.get("folder_id"),
        "logical_path": path, "title": item.get("title"),
        "action_type": action_type, "open_target": open_target,
    }


def _category_needs_classification(value):
    value = str(value or "").strip()
    return not value or len(value) > 24 or bool(re.search(r"[,，/|｜、]", value))


@app.post("/api/followups/classify")
def classify_followups(body: dict):
    pid = body.get("project_id")
    if not pid or not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    include_all = bool(body.get("include_all"))
    project_items = db.list_followups(project_id=pid)
    items = project_items if include_all else [
        x for x in project_items if _category_needs_classification(x.get("category"))
    ]
    if not items:
        return {"updated": 0, "remaining": 0, "categories": FOLLOWUP_CATEGORIES}
    classified = _classify_followup_items(items)
    updated = 0
    for item in items:
        category = classified.get(item["id"])
        if category and category != item.get("category"):
            db.update_followup(item["id"], {
                "category": category,
                "category_source": "ai",
                "previous_category": item.get("category"),
            })
            updated += 1
    if updated:
        _commit(f"AI 归类项目追问：{updated} 条")
    remaining = len([
        x for x in db.list_followups(project_id=pid)
        if _category_needs_classification(x.get("category"))
    ])
    return {
        "updated": updated,
        "remaining": remaining,
        "categories": FOLLOWUP_CATEGORIES,
        "learned_rules": len(_taxonomy_feedback()),
    }


_REFLECT_HINTS = ("不足", "缺点", "改进", "优化", "怎么改", "如果", "换成", "还能",
                  "反思", "局限", "更好", "为什么不", "假设", "扩展", "下一步", "trade")


def _guess_followup_kind(question: str) -> str:
    return "reflective" if any(k in (question or "") for k in _REFLECT_HINTS) else "factual"


@app.post("/api/followups/{fid}/polish")
def polish_followup(fid: int, body: dict):
    """类型感知的『和 AI 一起打磨答法』。读全项目背景；深挖型问你要真相、反思延伸型 AI 主动复盘。"""
    fol = db.get_followup(fid)
    if not fol:
        raise HTTPException(404, "追问不存在")
    if not db.get_project(fol.get("project_id")):
        raise HTTPException(404, "项目不存在")
    kind = (body.get("kind") or fol.get("kind") or _guess_followup_kind(fol.get("question"))).strip()
    action = (body.get("action") or "").strip()
    instruction = (body.get("instruction") or "").strip()

    ctx = caddie_context.build_context(project_id=fol.get("project_id"), intent="followup_answer")
    siblings = [x for x in db.list_followups(project_id=fol.get("project_id")) if x["id"] != fid]
    sib_text = "\n".join(
        f"- 问：{s.get('question')}\n  答：{(s.get('answer') or '（未答）')[:200]}" for s in siblings[:12]
    ) or "（无）"

    payload = f"""【这个项目的完整背景】
{ctx['text'][:8000]}

【同项目的其它追问及答法（保持一致、别矛盾或重复）】
{sib_text}

【这条追问】
{fol.get('question')}

【我目前的答法（可能为空或粗糙）】
{fol.get('answer') or '（空）'}
"""
    if instruction:
        payload += f"\n【我的补充/指令】\n{instruction}\n"
    if action:
        payload += f"\n【这次要做的】{action}\n"

    if kind == "reflective":
        system = """你是资深面试教练，帮求职者回答【反思/延伸类】追问（如"不足在哪""你会怎么改""如果换个场景"）。
这类问题用户往往没有现成答案，需要你主动复盘、产出高质量分析。要求：
- 必须站在【真实项目背景】上推理，结合他实际用的方法、数据规模、业务场景，不能空谈套话。
- 给出有洞察的内容：真实的不足、可行的改进方向、关键 trade-off。
- 推测性判断标成「（假设）」，由用户拍板，不要冒充事实。
- 结构清晰、可口语化背诵，用中文 Markdown。
直接输出打磨后的答法本身，不要解释你做了什么。"""
    else:
        system = """你是资深面试教练，帮求职者回答【深挖类】追问（如"具体怎么做的""你贡献多少"）。
答案在用户脑子里，你的任务是把他的真话组织成有力回答，绝不替他编造事实或数字。要求：
- 只用【背景】和【我的答法/补充】里出现的事实来组织，按 STAR 讲清楚。
- 缺关键具体细节（某个数字、某个做法）时，在该处插入醒目占位「（这里需要你补：……）」让用户填，不要瞎编。
- 结构清晰、可口语化背诵，用中文 Markdown。
直接输出打磨后的答法本身，不要解释你做了什么。"""

    try:
        out = ai.chat([{"role": "user", "content": payload}], system=system,
                      max_tokens=1600, profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"打磨失败：{str(e)[:200]}")
    if not fol.get("kind"):
        db.update_followup(fid, {"kind": kind})
    return {"answer": out, "kind": kind}


@app.get("/api/projects/{pid}/sources")
def project_sources(pid: int):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    out = []
    for link in db.list_source_links(entity_type="project", entity_id=pid):
        src = db.get_source(link.get("source_id"))
        if src:
            src = _refresh_linked_local_source(src)
            raw_path = src.get("file_path")
            path = Path(raw_path).expanduser() if raw_path else None
            exists = bool(path and path.exists() and path.is_file())
            suffix = path.suffix.lower() if path else Path(src.get("file_name") or "").suffix.lower()
            src["file_exists"] = exists
            src["file_ext"] = suffix.lstrip(".")
            src["file_size"] = path.stat().st_size if exists else None
            src["can_inline_preview"] = suffix in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
            out.append({"link": link, "source": src})
    return {"items": out}


@app.post("/api/projects/{pid}/sources/link-local")
def link_project_local_sources(pid: int, body: ProjectLocalSourcesIn):
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "项目不存在")
    linked, duplicates, errors = [], [], []
    for raw_path in dict.fromkeys(body.paths):
        path = Path(raw_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            errors.append({"path": raw_path, "error": "本地文件不存在或已移动"})
            continue
        if path.suffix.lower() not in CAREER_FILE_EXTENSIONS:
            errors.append({"path": str(path), "error": f"暂不支持 {path.suffix or '无扩展名'} 文件"})
            continue
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            existing = db.find_source_by_hash(digest)
            existing_path = None
            if existing and existing.get("file_path"):
                try:
                    existing_path = Path(existing["file_path"]).expanduser().resolve()
                except Exception:
                    existing_path = None
            if existing and existing_path == path:
                sid = existing["id"]
                duplicates.append({"id": sid, "path": str(path)})
            else:
                text = _extract_text(raw, path.name).strip()
                suffix = path.suffix.lower()
                source_type = "worklog" if suffix in {".xlsx", ".csv", ".tsv"} else (
                    "knowledge" if suffix == ".pptx" else "project"
                )
                sid = db.create_source({
                    "source_type": source_type,
                    "title": path.stem,
                    "content": text,
                    "file_name": path.name,
                    "file_path": str(path),
                    "content_hash": digest,
                    "origin": "local_reference",
                    "status": "ingested",
                    "ingested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                db.replace_search_document("source", sid, path.stem, text)
                _workspace_event("source_created", "source", sid, f"关联本地资料：{path.name}")
            db.create_source_link(sid, "project", pid, "evidence_for", "项目事实依据")
            linked.append({"id": sid, "path": str(path), "title": path.stem})
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)[:200]})
    if linked:
        _commit(f"关联项目本地资料 {len(linked)} 份")
    return {"linked": linked, "duplicates": duplicates, "errors": errors}


@app.get("/api/sources/{sid}/file")
def preview_source_file(sid: int):
    src = db.get_source(sid)
    if not src:
        raise HTTPException(404, "资料不存在")
    raw_path = src.get("file_path")
    path = Path(raw_path).expanduser() if raw_path else None
    if not path or not path.exists() or not path.is_file():
        raise HTTPException(404, "本地原文件不存在或已移动")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, headers={
        "Cache-Control": "no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(src.get('file_name') or path.name)}",
    })


@app.post("/api/sources/{sid}/open")
def open_source_file(sid: int, body: dict):
    src = db.get_source(sid)
    if not src:
        raise HTTPException(404, "资料不存在")
    raw_path = src.get("file_path")
    path = Path(raw_path).expanduser() if raw_path else None
    if not path or not path.exists() or not path.is_file():
        raise HTTPException(404, "本地原文件不存在或已移动")
    if (body.get("target") or "file") == "folder":
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["open", "-R", str(path)])
    else:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["open", str(path)])
    return {"ok": True}


@app.delete("/api/projects/{pid}/sources/{sid}")
def unlink_project_source(pid: int, sid: int):
    if not db.get_project(pid):
        raise HTTPException(404, "项目不存在")
    source = db.get_source(sid)
    if not source:
        raise HTTPException(404, "资料不存在")
    db.delete_source_link(sid, "project", pid)
    _commit(f"解除项目资料关联：{source.get('title') or source.get('file_name') or sid}")
    return {"ok": True}


@app.get("/api/projects/{pid}/readiness")
def project_readiness(pid: int):
    project = db.get_project(pid)
    if not project:
        raise HTTPException(404, "项目不存在")
    linked_sources = []
    for link in db.list_source_links(entity_type="project", entity_id=pid):
        src = db.get_source(link.get("source_id"))
        if src:
            linked_sources.append({"link": link, "source": src})
    return harness.compute_project_readiness(
        project,
        followups=db.list_followups(project_id=pid),
        assets=db.list_assets(project_id=pid, limit=200),
        sources=linked_sources,
    )


@app.get("/api/feedback")
def feedback_notes(scope: Optional[str] = None, scope_id: Optional[int] = None, status: Optional[str] = "active"):
    return {"items": db.list_feedback_notes(scope=scope, scope_id=scope_id, status=status)}


def _validate_feedback_scope(scope: str, scope_id: Optional[int]):
    scope = scope or "global"
    if scope == "global":
        return
    if not scope_id:
        return
    if scope == "track" and not db.get_job_track(scope_id):
        raise HTTPException(404, "反馈绑定的求职线不存在")
    if scope == "project" and not db.get_project(scope_id):
        raise HTTPException(404, "反馈绑定的项目不存在")
    if scope == "asset" and not db.get_asset(scope_id):
        raise HTTPException(404, "反馈绑定的资产不存在")
    if scope == "experience":
        exists = any(int(x.get("id")) == int(scope_id) for x in db.get_experiences())
        if not exists:
            raise HTTPException(404, "反馈绑定的经历不存在")


def _mark_feedback_stale_assets(normalized: dict, impact: dict) -> dict:
    scope = normalized.get("scope") or "global"
    scope_id = normalized.get("scope_id")
    if not impact.get("should_mark_stale"):
        return {"changed": 0, "scope": scope, "scope_id": scope_id}
    if scope == "asset" and scope_id:
        changed = db.mark_asset_stale(scope_id)
    elif scope == "track" and scope_id:
        changed = db.mark_assets_stale(track_id=scope_id)
    elif scope == "project" and scope_id:
        changed = db.mark_assets_stale(project_id=scope_id)
    elif scope == "experience" and scope_id:
        changed = 0
        exp = next((x for x in db.get_experiences() if int(x.get("id")) == int(scope_id)), None)
        for project in (exp or {}).get("projects") or []:
            changed += db.mark_assets_stale(project_id=project.get("id"))
    elif scope == "global":
        changed = db.mark_assets_stale(global_scope=True)
    else:
        changed = 0
    return {"changed": changed, "scope": scope, "scope_id": scope_id}


def _save_feedback_with_harness(body: FeedbackHarnessIn | dict) -> dict:
    data = body.dict() if hasattr(body, "dict") else dict(body or {})
    scope = data.get("scope") or "global"
    scope_id = data.get("scope_id")
    _validate_feedback_scope(scope, scope_id)
    normalized = harness.normalize_feedback_note(
        data.get("original_text") or data.get("directive") or data.get("content") or "",
        scope=scope, scope_id=scope_id, category=data.get("category"),
        polarity=data.get("polarity"), strength=data.get("strength"),
        directive=data.get("directive"),
    )
    report = normalized.pop("report")
    if not report.passed:
        raise HTTPException(400, "Feedback Harness 拦截：" + "；".join(
            x.message for x in report.issues if x.level == "error"
        ))
    impacted_assets = db.feedback_impacted_assets(scope=normalized["scope"], scope_id=normalized.get("scope_id"))
    impact = harness.feedback_impact_policy(normalized, impacted_assets)
    stale_result = _mark_feedback_stale_assets(normalized, impact) if data.get("apply_stale", True) else {"changed": 0}
    impact["stale_changed"] = stale_result.get("changed", 0)
    fid = db.create_feedback_note({
        **normalized,
        "note_type": data.get("note_type") or "feedback",
        "source_id": data.get("source_id"),
        "status": "active",
        "impact_json": json.dumps(impact, ensure_ascii=False),
        "normalized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    note = db.get_feedback_note(fid)
    return {
        "feedback": note,
        "normalized": normalized,
        "impact": impact,
        "report": report.to_dict(),
    }


@app.post("/api/feedback/harness")
def create_feedback_with_harness(body: FeedbackHarnessIn):
    result = _save_feedback_with_harness(body)
    stale = result["impact"].get("stale_changed") or 0
    label = result["normalized"].get("directive") or body.original_text[:40]
    suffix = f"，标记 {stale} 个资产待更新" if stale else ""
    _workspace_event("feedback_created", "feedback_note", result["feedback"]["id"],
                     "新增反馈约束", result["normalized"].get("scope") or "global",
                     result["normalized"].get("scope_id"))
    _commit(f"新增反馈约束：{label[:60]}{suffix}")
    return result


@app.delete("/api/feedback/{fid}")
def remove_feedback_note(fid: int):
    current = db.get_feedback_note(fid)
    if not db.delete_feedback_note(fid):
        raise HTTPException(404, "反馈不存在")
    _workspace_event("feedback_deleted", "feedback_note", fid, "删除反馈约束",
                     (current or {}).get("scope") or "global", (current or {}).get("scope_id"))
    _commit("删除反馈约束")
    return {"ok": True}


@app.get("/api/harness/catalog")
def harness_catalog():
    """Describe Caddie's guardrail/runtime harnesses for humans and agent clients."""
    return {
        "items": [
            {
                "key": "task_harness",
                "name": "Agent 任务 Harness",
                "entrypoints": ["/api/agent/tasks", "/api/agent/coordinate"],
                "purpose": "把后台专家任务变成可校验、可追踪、可返修的执行流程。",
                "guards": ["任务目标校验", "专家路由", "上下文预算", "输出质量审计", "自动返修", "候选修改确认"],
                "writes_data": "only_after_user_confirmed_changes",
            },
            {
                "key": "chat_harness",
                "name": "问 Caddie 对话 Harness",
                "entrypoints": ["/api/chat"],
                "purpose": "让即时对话也具备任务卡、执行契约、上下文边界和回答审计。",
                "guards": ["空任务拦截", "本地资料来源边界", "禁止假装写库", "回答结构与下一步检查", "必要时自动返修"],
                "writes_data": "never_directly",
            },
            {
                "key": "local_context_harness",
                "name": "本地资料读取 Harness",
                "entrypoints": ["/api/local-discovery/scan", "/api/chat"],
                "purpose": "让 Caddie 像 Codex 一样读取本地文件执行任务，同时避免默认污染资料库。",
                "guards": ["候选扫描", "内容去重", "文件上限", "临时上下文预算", "入库与临时读取分离"],
                "writes_data": "scan_candidates_only_until_import_confirmed",
            },
            {
                "key": "ingestion_harness",
                "name": "资料入库 Harness",
                "entrypoints": ["/api/local-discovery/ingestion-plan", "/api/local-discovery/import"],
                "purpose": "把本地候选资料入库前的风险、重复、低置信度和写入动作变成可确认报告。",
                "guards": ["入库预检", "重复跳过", "低置信度提示", "批量上限", "写入结果报告"],
                "writes_data": "only_after_user_import_confirmation",
            },
            {
                "key": "context_harness",
                "name": "上下文装配 Harness",
                "entrypoints": ["/api/harness/status", "context.build_context"],
                "purpose": "按求职线、项目、反馈、知识和检索结果装配上下文，并做预算裁剪。",
                "guards": ["反馈铁律优先", "岗位/项目范围隔离", "上下文预算", "来源 refs", "过期资产提示"],
                "writes_data": "read_only",
            },
            {
                "key": "feedback_harness",
                "name": "反馈约束 Harness",
                "entrypoints": ["/api/feedback/harness", "/api/chat/actions/apply"],
                "purpose": "把用户反馈转成后续生成必须遵守的约束，并标记受影响资产待更新。",
                "guards": ["反馈归一化", "作用域校验", "hard/soft 分类", "影响资产计算", "stale 标记"],
                "writes_data": "creates_feedback_note_and_marks_related_assets_stale",
            },
            {
                "key": "readiness_harness",
                "name": "准备度雷达 Harness",
                "entrypoints": ["/api/job-tracks/{tid}/readiness", "/api/projects/{pid}/readiness"],
                "purpose": "用确定性规则判断求职线和项目是否足够支撑投递、面试与后续生成。",
                "guards": ["JD 完整性", "差距闭环", "岗位简历", "知识卡", "项目追问", "证据来源", "过期资产"],
                "writes_data": "read_only",
            },
            {
                "key": "evaluation_harness",
                "name": "输出评测 Harness",
                "entrypoints": ["/api/harness/evaluate-output"],
                "purpose": "对 Caddie 或外部 agent 的输出做可重复质量评测，支持后续固定测试集和模型对比。",
                "guards": ["结构检查", "证据边界", "下一步行动", "过度承诺", "预期关键词覆盖", "最低分阈值"],
                "writes_data": "read_only",
            },
        ],
        "principles": [
            "人和 agent 都能用同一套入口。",
            "临时读取与正式入库分开。",
            "所有写库动作都必须经过用户确认。",
            "模型输出必须区分事实、推断和待确认信息。",
            "强模型负责判断，harness 负责流程、边界和质量门。",
        ],
    }


@app.post("/api/harness/evaluate-output")
def evaluate_harness_output(body: HarnessEvaluateIn):
    return harness.evaluate_output_case(
        body.text,
        title=body.title or "",
        expert_key=body.expert_key,
        mode=body.mode,
        expected_keywords=body.expected_keywords,
        min_score=body.min_score,
    )


@app.get("/api/harness/status")
def harness_status(track_id: Optional[int] = None, project_id: Optional[int] = None):
    """Expose the current guardrail state for humans and external agents."""
    ctx = caddie_context.build_context(
        track_id=track_id, project_id=project_id, intent="harness_status",
        query="当前求职资料、反馈约束、资产状态和上下文质量",
    )
    packed = harness.pack_context(ctx.get("text") or "", {
        "id": None, "task_type": "harness_status", "assigned_expert": "career_lead",
        "track_id": track_id, "object_type": "project" if project_id else None,
        "object_id": project_id,
    }, budget=18000)
    feedback = ctx.get("feedback") or []
    assets = db.list_assets(track_id=track_id, project_id=project_id, limit=200)
    stale_assets = [x for x in assets if x.get("status") == "stale"]
    hard_feedback = [x for x in feedback if (x.get("strength") or "").lower() == "hard"]
    soft_feedback = [x for x in feedback if (x.get("strength") or "").lower() != "hard"]
    def compact_feedback(item):
        text = item.get("directive") or item.get("content") or item.get("original_text") or ""
        return {
            "id": item.get("id"),
            "scope": item.get("scope"),
            "scope_id": item.get("scope_id"),
            "category": item.get("category") or item.get("note_type"),
            "polarity": item.get("polarity"),
            "strength": item.get("strength"),
            "directive": text[:320],
            "status": item.get("status"),
            "created_at": item.get("created_at"),
        }
    readiness_issues = []
    if track_id and not (db.get_job_track(track_id) or {}).get("jd"):
        readiness_issues.append({"level": "warning", "code": "missing_jd", "message": "求职线没有 JD，上下文会偏泛。"})
    if project_id:
        p = db.get_project(project_id)
        if p and len((p.get("document") or "").strip()) < 800:
            readiness_issues.append({"level": "warning", "code": "thin_project_doc", "message": "项目文档偏薄，生成时容易缺少证据。"})
    if stale_assets:
        readiness_issues.append({"level": "warning", "code": "stale_assets", "message": f"{len(stale_assets)} 个资产已过期，需要重生成或复核。"})
    readiness = None
    if track_id:
        t = db.get_job_track(track_id)
        if t:
            readiness = harness.compute_track_readiness(
                t,
                gaps=db.list_track_gaps(track_id),
                assets=assets,
                knowledge_items=db.list_knowledge_items(track_id=track_id),
                resume_versions=db.list_resume_versions(track_id),
                feedback=feedback,
            )
    elif project_id:
        p = db.get_project(project_id)
        if p:
            linked_sources = []
            for link in db.list_source_links(entity_type="project", entity_id=project_id):
                src = db.get_source(link.get("source_id"))
                if src:
                    linked_sources.append({"link": link, "source": src})
            readiness = harness.compute_project_readiness(
                p,
                followups=db.list_followups(project_id=project_id),
                assets=assets,
                sources=linked_sources,
            )
    return {
        "scope": {"track_id": track_id, "project_id": project_id},
        "context": {
            "refs_count": len(ctx.get("refs") or []),
            "feedback_count": len(feedback),
            "packed_chars": packed["used"],
            "truncated": packed["truncated"],
        },
        "feedback": {
            "hard": len(hard_feedback),
            "soft": len(soft_feedback),
            "items": [compact_feedback(x) for x in feedback[:30]],
        },
        "assets": {
            "total": len(assets),
            "stale": len(stale_assets),
            "stale_items": stale_assets[:30],
        },
        "readiness": readiness,
        "issues": readiness_issues,
    }


# ─── 项目资料上传：解析文件并并入项目文档 ────────────────────────────────────

PROJECT_FILE_MERGE_PROMPT = """用户为【某个项目】上传了一份资料文件，请把其中和这个项目相关的有用信息
整理成可并入项目的增量补丁，不要重写整篇文档。

硬规则：
1. 大段正文绝对不要放进 JSON。按分隔符格式返回。
2. 项目文档只保留：标题、一句话亮点、背景、我的动作、结果。
3. 资料里能预判到的面试追问，必须放到 FOLLOWUPS 区，不要写进文档。
4. 如果某区没有新增信息，留空。

当前项目文档：
%s

上传的资料文件「%s」内容：
%s

严格按此格式返回，不要 markdown 代码围栏，不要额外解释：
SUMMARY: 从这份资料里并入了什么（一句话）
ONE_LINER: 更新后的一句话亮点（没有就留空）
TECHNOLOGIES: 逗号分隔（没有就留空）
KEYWORDS: 逗号分隔（没有就留空）
===BACKGROUND===
新增/修正的背景段落
===ACTIONS===
新增/修正的“我的动作”
===RESULTS===
新增/修正的结果和量化影响
===FOLLOWUPS===
Q: 面试官可能追问的问题
A: 建议答法（没有就留空）
STATUS: todo/weak/stable
CATEGORY: 项目贡献/数据方法/业务理解/技术实现/结果量化/风险不足/其他
===END==="""


@app.post("/api/projects/{pid}/import")
async def project_import(pid: int, file: UploadFile = File(...)):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    raw = await file.read()
    try:
        text = _extract_text(raw, file.filename).strip()
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{str(e)[:150]}")
    if len(text) < 10:
        raise HTTPException(400, "没从文件里读到足够文字（可能是扫描版/图片型 PDF）。")
    prompt = PROJECT_FILE_MERGE_PROMPT % (p.get("document") or "（空）", file.filename or "资料", text[:12000])
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=3500,
                      profile_key="writing")
        data = _parse_project_patch(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"合并失败（模型返回无法解析）：{str(e)[:200]}")
    has_doc_patch = any((data.get(k) or "").strip() for k in ("one_liner", "technologies", "keywords", "background", "actions", "results"))
    changes = []
    if has_doc_patch:
        changes.append({
            "type": "update_project", "project_id": pid, "name": p.get("name"),
            "one_liner": data.get("one_liner") or p.get("one_liner"),
            "technologies": data.get("technologies") or p.get("technologies"),
            "keywords": data.get("keywords") or p.get("keywords"),
            "document": _merge_project_doc(p, data),
            "reason": data.get("summary"),
        })
    for f in data.get("followups") or []:
        f.update({"type": "create_followup", "project_id": pid, "origin": "project_file"})
        changes.append(f)
    if not changes:
        return {"summary": "没从资料里提取到可并入的内容", "changes": [], "chars": len(text)}
    return {"summary": data.get("summary", ""), "changes": changes, "chars": len(text)}


# ─── 简历 PDF 导入 ────────────────────────────────────────────────────────────

RESUME_PROMPT = """下面是用户上传的简历纯文本。请把它拆解成结构化的经历和项目，
返回可入库的改动建议。每段工作/实习 = 一个 create_experience，其下的每个项目放进它的 projects 数组。
项目的 document 用 Markdown 写好（含 标题、一句话亮点、背景、做了什么、结果），technologies 和 keywords 用逗号分隔。

简历文本：
%s

只返回如下 JSON，不要多余文字：
{
  "summary": "识别到 N 段经历、M 个项目",
  "changes": [
    {"type":"create_experience","company":"...","role":"...","start_date":"...","end_date":"...",
     "projects":[{"name":"...","one_liner":"...","document":"# ...","technologies":"...","keywords":"..."}]}
  ]
}"""


@app.post("/api/import/resume")
async def import_resume(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text = _extract_text(raw, file.filename).strip()
    except Exception as e:
        raise HTTPException(400, f"文件解析失败：{str(e)[:200]}")

    if len(text) < 20:
        raise HTTPException(400, "没从文件里读到足够的文字（可能是扫描版/图片型 PDF）。")

    try:
        out = ai.chat([{"role": "user", "content": RESUME_PROMPT % text[:12000]}],
                      max_tokens=4000, profile_key="fast")
        data = ai.extract_json(out)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"简历解析失败（模型返回不合规）：{str(e)[:200]}")
    data.setdefault("summary", "")
    data.setdefault("changes", [])
    data["chars"] = len(text)
    return data


# ─── 面试板块：自我介绍（版本管理）+ 模拟面试 ────────────────────────────────

class InterviewItemIn(StrictInputModel):
    kind: Literal["self_intro", "project_pitch", "knowledge", "knowledge_card", "other"] = "self_intro"
    title: Optional[str] = Field(None, max_length=500)
    target: Optional[str] = Field(None, max_length=500)
    content: Optional[str] = Field(None, max_length=300_000)
    project_id: Optional[int] = None


class GenIntroIn(BaseModel):
    target: str = ""
    length: str = "60秒"
    track_id: Optional[int] = None
    focus_context: Optional[str] = None


class MockIn(BaseModel):
    message: str = ""
    session_id: Optional[str] = None
    target: str = ""
    track_id: Optional[int] = None
    focus_context: Optional[str] = None


class DebriefIn(BaseModel):
    title: Optional[str] = None
    feedback: str
    project_id: Optional[int] = None
    track_id: Optional[int] = None
    hard_directive: Optional[str] = None


def _full_career_context(limit_chars: int = 6000) -> str:
    exps = db.get_experiences()
    if not exps:
        return "（用户还没有录入任何经历）"
    s = ""
    for e in exps:
        s += f"\n## {e['company']} · {e['role']}（{e.get('start_date','')} ~ {e.get('end_date','至今')}）\n"
        for p in e.get("projects", []):
            proj = db.get_project(p["id"]) or {}
            s += f"### 项目：{p['name']}\n{(proj.get('document') or '')[:800]}\n"
    return s[:limit_chars]


@app.get("/api/interview/items", deprecated=True)
def interview_items(kind: Optional[str] = None):
    return db.list_interview_items(kind)


@app.post("/api/interview/items", deprecated=True)
def add_interview_item(body: InterviewItemIn):
    try:
        iid = db.create_interview_item(body.dict())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    asset_id = None
    asset_type = {
        "self_intro": "self_intro",
        "project_pitch": "project_pitch",
        "knowledge": "knowledge_card",
        "knowledge_card": "knowledge_card",
    }.get(body.kind)
    if asset_type:
        asset_id = db.create_asset({
            "asset_type": asset_type,
            "project_id": body.project_id,
            "title": body.title or ("自我介绍" if body.kind == "self_intro" else "知识卡"),
            "body": body.content or "",
            "status": "draft",
            "provenance_json": json.dumps({
                "intent": "save_interview_item",
                "interview_item_id": iid,
                "target": body.target,
            }, ensure_ascii=False),
        })
    _commit(f"新增面试制品：{body.title or body.kind}")
    return {"id": iid, "asset_id": asset_id}


@app.put("/api/interview/items/{iid}", deprecated=True)
def edit_interview_item(iid: int, body: InterviewItemIn):
    if not db.update_interview_item(iid, body.dict()):
        raise HTTPException(404, "面试制品不存在")
    _commit(f"编辑面试制品：{body.title or ''}")
    return {"ok": True}


@app.delete("/api/interview/items/{iid}", deprecated=True)
def del_interview_item(iid: int):
    if not db.delete_interview_item(iid):
        raise HTTPException(404, "面试制品不存在")
    _commit("删除面试制品")
    return {"ok": True}


@app.post("/api/interview/self-intro")
def gen_self_intro(body: GenIntroIn):
    track = db.get_job_track(body.track_id) if body.track_id else None
    gaps = db.list_track_gaps(body.track_id) if body.track_id else []
    focus_context = (body.focus_context or "").strip()
    track_context = ""
    if track:
        gap_text = "\n".join(
            f"- {g.get('dimension') or ''}：{g.get('requirement') or ''} / {g.get('note') or ''}"
            for g in gaps[:10]
        ) or "（暂无差距项）"
        track_context = f"""
【当前岗位上下文】
公司：{track.get('company') or ''}
岗位：{track.get('role') or track.get('target') or ''}
JD：{(track.get('jd') or '')[:3000]}
差距项：
{gap_text}
"""
    if focus_context:
        track_context += f"\n【本岗位面试错题集/考点库】\n{focus_context[:3000]}\n"
    prompt = f"""根据下面这个人的【真实经历】，写一段面试用的【自我介绍】口语稿。
目标岗位/行业：{body.target or '通用'}
时长：{body.length}（据此控制字数，口语化、自然、有重点；突出和目标最相关的经历与量化成果；不浮夸、不编造）。
结构：一句话定位 → 1~2 段最相关经历&成果 → 为什么适合/对它感兴趣。
如果提供了岗位上下文和错题/考点，要优先回应这些高风险考点：把面试官可能追问的风险前置化解，但不要机械堆关键词。

{track_context}
【他的经历】{_full_career_context()}

直接输出自我介绍正文，不要任何解释或标题。"""
    try:
        text = ai.chat([{"role": "user", "content": prompt}], max_tokens=1200,
                       profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"生成失败：{str(e)[:200]}")
    return {"content": text}


MOCK_SYSTEM = """你是一位资深、专业但友善的面试官，正在面试这位求职者。规则：
- 一次只问一个问题，问完就停，等他回答，绝不自问自答。
- 紧扣他的真实经历深入追问：背景 / 你具体做了什么 / 量化结果 / 技术细节 / 权衡取舍 / 难点怎么解决。
- 他回答后，先用一两句简短点评（亮点或不足），再追问下一个问题。
- 适时穿插行为面试题或岗位相关开放题，保持真实面试节奏，有适度压迫感但不刁难。
- 全程中文。第一次发言先简短欢迎，然后直接问第一个问题。"""


@app.post("/api/interview/mock")
def mock_interview(body: MockIn):
    sid = body.session_id or ("mock-" + uuid.uuid4().hex)
    history = db.get_chat_history(sid, limit=20)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": body.message or "（开始面试，请提第一个问题）"})
    track = db.get_job_track(body.track_id) if body.track_id else None
    gaps = db.list_track_gaps(body.track_id) if body.track_id else []
    focus_context = (body.focus_context or "").strip()
    track_context = ""
    if track:
        track_context = f"\n公司：{track.get('company') or ''}\n岗位：{track.get('role') or track.get('target') or ''}\nJD：{(track.get('jd') or '')[:2500]}\n"
        if gaps:
            track_context += "\n岗位差距项：\n" + "\n".join(
                f"- {g.get('dimension') or ''}：{g.get('requirement') or ''} / {g.get('note') or ''}"
                for g in gaps[:10]
            )
    if focus_context:
        track_context += f"\n\n本岗位面试错题集/考点库（优先围绕这些高风险考点追问）：\n{focus_context[:3000]}"
    system = MOCK_SYSTEM + f"\n本次目标岗位：{body.target or '通用'}\n{track_context}\n\n【求职者的经历】" + _full_career_context()
    try:
        reply = ai.chat(messages, system=system, max_tokens=1200,
                        profile_key="deep_reasoning")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"调用 AI 失败：{str(e)[:200]}")
    db.save_message(sid, "user", body.message or "（开始面试）")
    db.save_message(sid, "assistant", reply)
    return {"reply": reply, "session_id": sid}


DEBRIEF_PROMPT = """你是面试复盘助手。根据用户提供的真实面试反馈，提取可以回流到求职知识库的内容。

规则：
1. 只提取反馈里真实出现或明确暴露的问题，不编造面试官没问过的问题。
2. followups 是之后要继续准备的项目追问；status 用 weak（答得不稳）或 todo（还没答）。
3. constraints 是以后生成内容时应遵守的软约束，例如表达偏好、强调重点、需要避免的说法。
4. 事实纠正和主导权问题不要擅自升级为 hard；用户会在单独字段明确确认。
5. 返回内容较短，严格使用 JSON。

返回：
{
  "summary": "本轮复盘的一句话结论",
  "followups": [
    {"question":"问题","answer":"反馈中已有的答法，没有则为空","status":"weak","category":"项目贡献"}
  ],
  "constraints": [
    {"directive":"以后生成时应遵守的指令","category":"tone/emphasis/avoidance/interviewer_probe"}
  ]
}

面试反馈：
%s"""


@app.post("/api/interview/debrief")
def save_interview_debrief(body: DebriefIn):
    feedback = (body.feedback or "").strip()
    hard = (body.hard_directive or "").strip()
    if len(feedback) < 10:
        raise HTTPException(400, "复盘内容太短")
    if body.project_id and not db.get_project(body.project_id):
        raise HTTPException(404, "关联项目不存在")
    if body.track_id and not db.get_job_track(body.track_id):
        raise HTTPException(404, "关联求职线不存在")
    try:
        raw = ai.chat(
            [{"role": "user", "content": DEBRIEF_PROMPT % feedback[:8000]}],
            max_tokens=1500, profile_key="deep_reasoning",
        )
        data = ai.extract_json(raw)
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"复盘提取失败：{str(e)[:180]}")

    title = (body.title or "").strip() or "面试复盘"
    aid = db.create_asset({
        "asset_type": "review",
        "project_id": body.project_id,
        "track_id": body.track_id,
        "title": title,
        "body": feedback,
        "status": "final",
        "provenance_json": json.dumps({
            "intent": "interview_debrief",
            "project_id": body.project_id,
            "track_id": body.track_id,
        }, ensure_ascii=False),
    })

    followup_ids = []
    if body.project_id:
        for item in (data.get("followups") or [])[:20]:
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            status = item.get("status") if item.get("status") in ("weak", "todo") else "weak"
            followup_ids.append(db.create_followup(body.project_id, {
                "question": question,
                "answer": str(item.get("answer") or "").strip(),
                "status": status,
                "category": str(item.get("category") or "").strip() or None,
                "origin": title,
                "source_track_id": body.track_id,
                "category_source": "ai" if item.get("category") else None,
            }))

    feedback_ids = []
    constraint_scope = "track" if body.track_id else ("project" if body.project_id else "global")
    constraint_scope_id = body.track_id or body.project_id
    for item in (data.get("constraints") or [])[:12]:
        directive = str(item.get("directive") or "").strip()
        if not directive:
            continue
        feedback_ids.append(db.create_feedback_note({
            "scope": constraint_scope,
            "scope_id": constraint_scope_id,
            "category": str(item.get("category") or "interviewer_probe"),
            "polarity": "do",
            "strength": "soft",
            "directive": directive,
            "content": directive,
            "original_text": feedback,
        }))

    hard_id = None
    if hard:
        hard_id = db.create_feedback_note({
            "scope": "global",
            "category": "fact_correction",
            "polarity": "avoid",
            "strength": "hard",
            "directive": hard,
            "content": hard,
            "original_text": hard,
        })
        stale = db.mark_assets_stale(global_scope=True, exclude_asset_id=aid)
    else:
        stale = db.mark_assets_stale(
            project_id=body.project_id,
            track_id=body.track_id,
            exclude_asset_id=aid,
        )

    knowledge_id = None
    if body.track_id:
        folders, _ = db.ensure_track_knowledge_folders(body.track_id)
        folder = next((item for item in folders if item.get("name") == "面试复盘"), None)
        if folder:
            followup_lines = []
            for item in (data.get("followups") or [])[:20]:
                question = str(item.get("question") or "").strip()
                if question:
                    followup_lines.append(f"- {question}")
            constraint_lines = []
            for item in (data.get("constraints") or [])[:12]:
                directive = str(item.get("directive") or "").strip()
                if directive:
                    constraint_lines.append(f"- {directive}")
            review_body = (
                f"# {title}\n\n"
                f"> {data.get('summary') or '本轮面试复盘'}\n\n"
                "## 原始记录 / 逐字稿\n\n"
                f"{feedback}\n\n"
                "## 暴露的问题与追问\n\n"
                f"{chr(10).join(followup_lines) or '- 暂未提取到明确追问'}\n\n"
                "## 下一轮要遵守的改进\n\n"
                f"{chr(10).join(constraint_lines) or '- 继续补充可执行的改进动作'}\n\n"
                "## 深度复盘\n\n"
                "### 行为与临场\n\n待补充。\n\n"
                "### 表达结构与语言\n\n待补充。\n\n"
                "### 项目与专业深度\n\n待补充。\n\n"
                "### 面试官意图\n\n待补充。\n"
            )
            knowledge_id = db.create_knowledge_item({
                "title": title, "content": review_body, "scope_type": "track",
                "track_id": body.track_id, "folder_id": folder["id"],
                "topic": "面试复盘", "mastery": "learning", "status": "active",
                "source_type": "debrief", "source_ref_id": aid,
            })

    _commit(f"面试复盘回流：{title}")
    return {
        "ok": True,
        "summary": data.get("summary") or "复盘已回流",
        "asset_id": aid,
        "followup_ids": followup_ids,
        "feedback_ids": feedback_ids,
        "hard_feedback_id": hard_id,
        "stale_assets": stale,
        "knowledge_id": knowledge_id,
    }


# ─── 总览 Dashboard ───────────────────────────────────────────────────────────

@app.get("/api/overview")
def overview():
    data = db.overview_stats()
    data["recent"] = vcs.log(8)
    active = ai.get_active_provider()
    data["ai"] = active.get("name") if active else None
    return data


@app.get("/api/ops/brief")
def ops_brief():
    data = db.ops_brief()
    active = ai.get_active_provider()
    data["ai"] = active.get("name") if active else None
    return data


# ─── 在职日记 ─────────────────────────────────────────────────────────────────

class WorkLogIn(BaseModel):
    log_date: Optional[str] = None
    experience_id: Optional[int] = None
    content: str


@app.get("/api/worklogs")
def worklogs(experience_id: Optional[int] = None):
    return db.list_work_logs(experience_id)


@app.get("/api/worklog", deprecated=True)
def worklogs_legacy(experience_id: Optional[int] = None):
    return worklogs(experience_id)


@app.post("/api/worklogs")
def add_worklog(body: WorkLogIn):
    _require_feature_available("legacy_worklog")
    wid = db.create_work_log(body.dict())
    _commit("新增在职日记")
    return {"id": wid}


@app.delete("/api/worklogs/{wid}")
def del_worklog(wid: int):
    _require_feature_available("legacy_worklog")
    db.delete_work_log(wid)
    _commit("删除一条在职日记")
    return {"ok": True}


@app.post("/api/worklogs/summary")
def worklog_summary(body: dict):
    _require_feature_available("legacy_worklog")
    eid = body.get("experience_id")
    logs = db.list_work_logs(eid)
    if not logs:
        raise HTTPException(400, "这段经历还没有日记，先记几条。")
    text = "\n".join(f"[{l['log_date']}] {l['content']}" for l in reversed(logs))
    prompt = f"""下面是某人一段实习/工作期间，按天记录的工作日记。请帮他：
1. 提炼这段经历真正的【价值与亮点】（量化成果、能力体现、对业务的影响）。
2. 指出可以包装进简历/面试的 2-3 个【项目级故事】（每个给一句话概括 + 关键数据）。
3. 给出还值得补充记录的方向（哪些没写清、面试可能会追问）。

日记：
{text[:8000]}

用中文，条理清晰，直接给结论。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1500,
                      profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"summary": out}


WORKLOG_EXTRACT_PROMPT = """你是求职项目梳理助手。请从下面同一段经历的工作日记中识别 1-3 个真正能独立讲清楚的项目候选。

拆分原则：
1. 同一个业务目标、同一套连续动作应合并成一个项目，不要按每天或每个小任务拆碎。
2. 只使用日记里的事实，不编造职责、技术、数字或成果。
3. 项目文档只包含背景、我的动作、结果；面试追问不写进文档。
4. 没有量化证据时，在 MISSING_METRICS 提出具体待补数据，不要替用户编数字。
5. COVERED_LOG_IDS 只能填写输入中真实存在的日志 id。
6. 大段正文不要放进 JSON，严格使用下面的分隔符格式。

每个候选严格按此格式输出，可重复 1-3 次：
===DIGEST===
TITLE: 项目名
ONE_LINER: 一句话说明做了什么和价值；没有可靠结果时不要伪造
TECHNOLOGIES: 工具或方法，逗号分隔
KEYWORDS: 业务关键词，逗号分隔
COVERED_LOG_IDS: 1,2,3
MISSING_METRICS: 待补数据1 | 待补数据2
===BACKGROUND===
用自然段写清业务背景、问题和目标。
===ACTIONS===
写清用户本人做了什么。适合叙述的用自然段，步骤或方法可以用要点。
===RESULTS===
只写有依据的结果；若日记尚无结果，明确写“结果待补充”。
===END_DIGEST===

工作日记：
%s"""


def _parse_int_list(value: str):
    return [int(x) for x in re.findall(r"\d+", value or "")]


def _parse_worklog_digests(raw: str, allowed_log_ids):
    allowed = {int(x) for x in allowed_log_ids}
    chunks = re.findall(r"(?s)===DIGEST===\s*(.*?)\s*===END_DIGEST===", raw or "")
    digests = []
    for chunk in chunks:
        title = _field_line(chunk, "TITLE")
        if not title:
            continue
        one_liner = _field_line(chunk, "ONE_LINER")
        background = _block(chunk, "BACKGROUND")
        actions = _block(chunk, "ACTIONS")
        results = _block(chunk, "RESULTS")
        covered = [x for x in _parse_int_list(_field_line(chunk, "COVERED_LOG_IDS")) if x in allowed]
        missing = [
            x.strip() for x in re.split(r"[|｜]", _field_line(chunk, "MISSING_METRICS"))
            if x.strip() and x.strip().lower() not in ("无", "none", "暂无")
        ]
        document = "\n\n".join([
            f"# {title}",
            f"> {one_liner}" if one_liner else "",
            "## 背景\n" + (background or "（待补充）"),
            "## 我的动作\n" + (actions or "（待补充）"),
            "## 结果\n" + (results or "（待补充）"),
        ]).replace("\n\n\n", "\n\n").strip()
        digests.append({
            "title": title,
            "one_liner": one_liner,
            "technologies": _field_line(chunk, "TECHNOLOGIES"),
            "keywords": _field_line(chunk, "KEYWORDS"),
            "document": document,
            "background": background,
            "actions": actions,
            "results": results,
            "covered_log_ids": covered,
            "missing_metrics": missing,
        })
    return digests


def _worklog_digest_asset_body(digests):
    parts = ["# 在职日记提炼"]
    for item in digests:
        parts.extend([
            f"## {item['title']}",
            item.get("one_liner") or "",
            f"覆盖日记：{', '.join('#' + str(x) for x in item.get('covered_log_ids') or []) or '未标注'}",
            "待补数据：" + ("、".join(item.get("missing_metrics") or []) or "暂无"),
        ])
    return "\n\n".join(x for x in parts if x)


@app.post("/api/worklogs/extract")
def extract_worklogs(body: dict):
    _require_feature_available("legacy_worklog")
    eid = body.get("experience_id")
    if not eid:
        raise HTTPException(400, "请先选择一段经历，再提炼项目。")
    experience = next((x for x in db.get_experiences() if x["id"] == int(eid)), None)
    if not experience:
        raise HTTPException(404, "经历不存在")
    logs = db.list_work_logs(int(eid))
    if not logs:
        raise HTTPException(400, "这段经历还没有日记，先记几条。")
    text = "\n".join(
        f"[log_id={item['id']} | {item.get('log_date') or '日期未知'}]\n{item.get('content') or ''}"
        for item in reversed(logs)
    )
    try:
        raw = ai.chat(
            [{"role": "user", "content": WORKLOG_EXTRACT_PROMPT % text[:12000]}],
            max_tokens=3200, profile_key="fast",
        )
        digests = _parse_worklog_digests(raw, [x["id"] for x in logs])
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"提炼失败（模型返回无法解析）：{str(e)[:200]}")
    if not digests:
        raise HTTPException(500, "提炼失败：模型没有返回可识别的项目候选，请重试。")
    all_log_ids = sorted({wid for item in digests for wid in item["covered_log_ids"]})
    asset_id = db.create_asset({
        "asset_type": "worklog_digest",
        "title": f"{experience['company']} · {experience['role']} 日记提炼",
        "body": _worklog_digest_asset_body(digests),
        "status": "draft",
        "provenance_json": json.dumps({
            "intent": "worklog_extract",
            "experience_id": int(eid),
            "work_log_ids": all_log_ids,
        }, ensure_ascii=False),
        "derived_from_json": json.dumps(
            [{"type": "work_log", "id": wid} for wid in all_log_ids],
            ensure_ascii=False,
        ),
    })
    _commit(f"提炼在职日记：识别 {len(digests)} 个项目候选")
    return {"digests": digests, "asset_id": asset_id}


@app.post("/api/worklogs/adopt")
def adopt_worklog_digest(body: dict):
    _require_feature_available("legacy_worklog")
    eid = body.get("experience_id")
    digest = body.get("digest") or {}
    mode = body.get("mode") or "create"
    project_id = body.get("project_id")
    if not eid or not digest.get("title"):
        raise HTTPException(400, "缺少经历或项目候选")
    if mode not in ("create", "merge"):
        raise HTTPException(400, "不支持的采纳方式")
    if mode == "merge":
        project = db.get_project(project_id)
        if not project or project.get("experience_id") != int(eid):
            raise HTTPException(400, "请选择当前经历下的项目")
        digest = dict(digest)
        digest["document"] = _merge_project_doc(project, {
            "one_liner": digest.get("one_liner"),
            "background": digest.get("background"),
            "actions": digest.get("actions"),
            "results": digest.get("results"),
        })
        digest["technologies"] = ", ".join(filter(None, [
            project.get("technologies"),
            digest.get("technologies"),
        ]))
    try:
        result = db.adopt_worklog_digest(
            int(eid), digest, mode=mode,
            project_id=int(project_id) if project_id else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    label = digest.get("title") or "项目候选"
    _commit(("并入项目：" if mode == "merge" else "从日记新建项目：") + label)
    return {"ok": True, **result}


# ─── 知识库（面试用专业知识卡）────────────────────────────────────────────────

class KnowledgeIn(BaseModel):
    topic: str
    target: Optional[str] = ""


class ProjectPitchIn(BaseModel):
    track_id: Optional[int] = None
    focus_context: Optional[str] = None


@app.post("/api/interview/knowledge")
def gen_knowledge(body: KnowledgeIn):
    prompt = f"""为面试准备，写一张关于「{body.topic}」的【知识卡】{('，结合岗位：'+body.target) if body.target else ''}。
要求：面向面试问答，讲清楚——是什么 / 为什么/什么场景用 / 关键点和坑 / 面试常见追问及简洁答法。
用中文 Markdown，结构清晰、精炼，能直接背。直接输出卡片内容。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1600,
                      profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"content": out}


# ─── 项目话术（每个项目的面试讲法）────────────────────────────────────────────

@app.post("/api/projects/{pid}/pitch")
def gen_project_pitch(pid: int, body: Optional[ProjectPitchIn] = None):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    ctx = caddie_context.build_context(project_id=pid, intent="project_pitch")
    body = body or ProjectPitchIn()
    track = db.get_job_track(body.track_id) if body.track_id else None
    gaps = db.list_track_gaps(body.track_id) if body.track_id else []
    track_context = ""
    if track:
        track_context = f"""
【当前目标岗位】
公司：{track.get('company') or ''}
岗位：{track.get('role') or track.get('target') or ''}
JD：{(track.get('jd') or '')[:2800]}
差距项：
{chr(10).join(f"- {g.get('dimension') or ''}：{g.get('requirement') or ''} / {g.get('note') or ''}" for g in gaps[:10]) or '（暂无）'}
"""
    if body.focus_context:
        track_context += f"\n【本岗位面试错题集/考点库】\n{body.focus_context[:3000]}\n"
    prompt = f"""请基于下面的【统一上下文】写一份【项目面试讲法话术】。

{ctx["text"][:9000]}

{track_context}

请产出：
## 30 秒电梯版（口语，突出成果和你的角色）
## 2 分钟详述版（背景-任务-行动-结果，STAR，带数字）
## 高频追问 & 我的答法（列 4-6 个面试官最可能追问的点，每个给简洁有力的答法）

要求：
- 用中文 Markdown，口语化、可直接背。
- 只基于上下文中的事实，不编造数字、职责或成果。
- 严格遵守【生效反馈约束】，尤其是 hard/铁律类约束。
- 如果提供了目标岗位和错题/考点，要让话术主动回应这些岗位关切；同一项目可以因岗位不同而突出不同侧面。
"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=2200,
                      profile_key="writing")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    aid = db.create_asset({
        "asset_type": "project_pitch",
        "project_id": pid,
        "title": (p.get("name") or "项目") + " · 面试话术",
        "body": out,
        "status": "draft",
        "provenance_json": caddie_context.provenance_payload(ctx),
    })
    _commit(f"生成项目话术草稿：{p.get('name') or pid}")
    return {"content": out, "title": p.get("name"), "project_id": pid, "asset_id": aid}


# ─── JD 对比（岗位描述 vs 你的画像，找差距）──────────────────────────────────

class JDIn(BaseModel):
    job_description: str
    analyze: bool = True  # False = 只存 JD，不跑 AI 分析


@app.post("/api/applications/{aid}/jd-analyze")
def jd_analyze(aid: int, body: JDIn):
    app_data = db.get_application(aid)
    if not app_data:
        raise HTTPException(404, "投递不存在")
    jd = body.job_description.strip()
    if len(jd) < 10:
        raise HTTPException(400, "JD 内容太短")
    if not body.analyze:
        db.set_application_jd(aid, jd)
        _commit(f"存储 JD：{app_data.get('company')}")
        return {"analysis": ""}
    prompt = f"""把这份岗位 JD 和求职者的真实画像做对比分析。
岗位：{app_data.get('company')} · {app_data.get('role')}
JD：
{jd[:4000]}

求职者画像（经历/项目）：
{_full_career_context()}

    请输出（中文 Markdown）：
## ✅ 匹配点（他已经具备、可重点突出的）
## ⚠️ 差距 / 缺口（JD 要求但他画像里弱或没有的）
## 📚 该补的知识/准备（针对缺口，具体到知识点或要准备的故事）
## 🎯 投递/面试建议（一句话该怎么扬长避短）
    基于事实，别编造他没有的经历。"""
    try:
        provider = ai.resolve_model_profile("research").get("provider") or {}
        analysis = ai.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=1800,
            provider={**provider, "_fallback_providers": []},
            timeout=25,
        )
    except ai.AIError as e:
        raise HTTPException(503, f"JD 分析暂时不可用：{str(e)[:240]}") from e
    db.set_application_jd(aid, jd, analysis)
    _commit(f"JD 对比分析：{app_data.get('company')}")
    return {"analysis": analysis}


@app.post("/api/applications/{aid}/jd-actions")
def jd_actions(aid: int):
    app_data = db.get_application(aid)
    if not app_data:
        raise HTTPException(404, "投递不存在")
    jd = (app_data.get("job_description") or "").strip()
    analysis = (app_data.get("gap_analysis") or "").strip()
    if not jd:
        raise HTTPException(400, "这条投递还没有 JD，先粘贴 JD。")
    if not analysis:
        raise HTTPException(400, "还没有差距分析，先点「对比分析」。")
    prompt = f"""你是求职操作系统的任务规划器。请把下面的 JD 差距分析转成一份可执行的行动清单。

岗位：{app_data.get('company')} · {app_data.get('role')}
JD 摘要：
{jd[:1800]}

已有差距分析：
{analysis[:5000]}

输出中文 Markdown，必须包含这 4 个部分：
## 简历要改
- 3-5 条，写清楚改哪段经历/项目、补什么证据或数据
## 面试要练
- 5-8 个高频追问，按优先级排序
## 知识要补
- 3-6 个知识卡主题，每个说明为什么要补
## 今天就做
- 3 个 30 分钟内能完成的小动作

要求：具体、可执行，不要泛泛而谈，不要编造用户没有的经历。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1800,
                      profile_key="research")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"actions": out}


# ─── 双视角自查（求职者视角 ↔ 面试官视角）─────────────────────────────────────

class ReviewIn(BaseModel):
    text: str
    context: Optional[str] = ""


@app.post("/api/review")
def dual_review(body: ReviewIn):
    if len(body.text.strip()) < 10:
        raise HTTPException(400, "内容太短")
    prompt = f"""下面是求职者的一段材料{('（'+body.context+'）') if body.context else ''}。请从两个视角做"自查"，帮他打磨：

材料：
{body.text[:5000]}

输出（中文 Markdown）：
## 🙋 求职者视角（怎么讲更打动人、更清楚）
- 哪里可以更突出成果/数据、更有说服力；表达上怎么改更好（给具体改法）。
## 🧐 面试官视角（会怎么质疑、追问、挑漏洞）
- 面试官看到这段会追问哪些问题？哪里听起来可疑或站不住脚？他需要提前准备什么？
## ✍️ 一句话总结：最该改的一点
直接、具体、可操作。"""
    try:
        out = ai.chat([{"role": "user", "content": prompt}], max_tokens=1600,
                      profile_key="deep_reasoning")
    except ai.AIError as e:
        raise HTTPException(400, str(e))
    return {"review": out}


# ─── 邮箱接入（网易 163/126 IMAP，投递邮件按时间整理）────────────────────────

EMAIL_CFG = ai.CONFIG_DIR / "email.json"
EMAIL_FETCH_CACHE = ai.CONFIG_DIR / "email_fetch_cache.json"
EMAIL_ANALYSIS_CACHE = ai.CONFIG_DIR / "email_analysis_cache.json"
EMAIL_PREFERENCES = ai.CONFIG_DIR / "email_preferences.json"
EMAIL_FETCH_CACHE_SECONDS = 5 * 60
_email_scheduler_started = False


class EmailCfgIn(BaseModel):
    provider: str = "netease"
    host: str = "imap.163.com"
    port: int = 993
    user: str
    authcode: str = ""
    clear: bool = False


class EmailMessageStateIn(StrictInputModel):
    message_key: str = Field(..., min_length=8, max_length=200)
    status: Literal["archived", "ignored"]
    subject: Optional[str] = Field(None, max_length=1000)
    sender: Optional[str] = Field(None, max_length=500)
    message_date: Optional[str] = Field(None, max_length=64)
    application_id: Optional[int] = None


class EmailPreferencesIn(StrictInputModel):
    days: int = Field(30, ge=1, le=365)
    schedule_enabled: bool = False
    schedule_time: str = "09:00"
    notifications_enabled: bool = True

    @validator("schedule_time")
    def validate_schedule_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
            raise ValueError("定时同步时间必须是 HH:MM")
        return value


def _email_cfg():
    if EMAIL_CFG.exists():
        try:
            return json.loads(EMAIL_CFG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _read_email_cache(path: Path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_email_cache(path: Path, value: dict):
    ai.CONFIG_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _email_cache_key(days: int, q: str = ""):
    cfg = _email_cfg()
    return hashlib.sha256(
        f"{cfg.get('user','')}|{cfg.get('host','')}|{days}|{q.strip()}".encode("utf-8")
    ).hexdigest()


def _email_preferences():
    saved = _read_email_cache(EMAIL_PREFERENCES)
    return {
        "days": max(1, min(int(saved.get("days") or 30), 365)),
        "schedule_enabled": bool(saved.get("schedule_enabled", False)),
        "schedule_time": saved.get("schedule_time") or "09:00",
        "notifications_enabled": bool(saved.get("notifications_enabled", True)),
        "last_run_date": saved.get("last_run_date"),
        "last_run_at": saved.get("last_run_at"),
        "last_new_count": int(saved.get("last_new_count") or 0),
        "seen_ids": list(saved.get("seen_ids") or [])[-500:],
    }


def _next_email_sync_at(preferences=None):
    preferences = preferences or _email_preferences()
    if not preferences.get("schedule_enabled"):
        return None
    hour, minute = map(int, preferences["schedule_time"].split(":"))
    now = datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.strftime("%Y-%m-%d %H:%M")


def _notify_new_career_email(count: int):
    if count <= 0:
        return
    title = "Caddie 发现新的求职邮件"
    body = f"新增 {count} 封求职相关邮件，打开邮箱查看并处理。"
    if sys.platform == "darwin":
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'],
                capture_output=True, timeout=8,
            )
        except Exception:
            pass


def _run_scheduled_email_sync():
    prefs = _email_preferences()
    if not prefs.get("schedule_enabled") or not _email_cfg().get("authcode"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if prefs.get("last_run_date") == today:
        return
    now_hm = datetime.now().strftime("%H:%M")
    if now_hm < prefs.get("schedule_time", "09:00"):
        return
    try:
        result = email_fetch(days=prefs["days"], force=True)
        current_ids = [
            item.get("id") for item in result.get("items", [])
            if item.get("job") and item.get("id")
        ]
        seen = set(prefs.get("seen_ids") or [])
        # 第一次启用只建立基线，避免把全部历史邮件当作“新增”轰炸通知。
        new_ids = [] if not prefs.get("last_run_at") and not seen else [
            item_id for item_id in current_ids if item_id not in seen
        ]
        prefs.update({
            "last_run_date": today,
            "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_new_count": len(new_ids),
            "seen_ids": list(dict.fromkeys(current_ids + list(seen)))[-500:],
        })
        _write_email_cache(EMAIL_PREFERENCES, prefs)
        by_id = {item.get("id"): item for item in result.get("items", [])}
        account = (_email_cfg().get("user") or "").strip()
        for message_id in new_ids:
            item = by_id.get(message_id) or {}
            db.create_email_reminder(message_id, {
                "account": account,
                "title": item.get("subject") or "新的求职邮件",
                "detail": f"{item.get('from') or '未知发件人'} · {item.get('date') or ''}".strip(" ·"),
            })
        if new_ids and prefs.get("notifications_enabled"):
            _notify_new_career_email(len(new_ids))
    except Exception:
        # 网络或邮箱暂时不可用时不写 last_run_date，下一轮继续尝试。
        return


def _email_scheduler_loop():
    while True:
        _run_scheduled_email_sync()
        time.sleep(30)


def _start_email_scheduler():
    global _email_scheduler_started
    if _email_scheduler_started:
        return
    _email_scheduler_started = True
    threading.Thread(
        target=_email_scheduler_loop,
        name="caddie-email-scheduler",
        daemon=True,
    ).start()


@app.get("/api/email/config")
def email_config():
    c = _email_cfg()
    return {
        "provider": c.get("provider", "netease"),
        "host": c.get("host", "imap.163.com"),
        "port": c.get("port", 993),
        "user": c.get("user", ""),
        "has_pass": bool(c.get("authcode")),
    }


@app.post("/api/email/config")
def save_email_config(body: EmailCfgIn):
    c = _email_cfg()
    # clear=断开（清空授权码）；否则授权码留空＝保留原值（方便只改邮箱）
    authcode = "" if body.clear else (body.authcode or c.get("authcode", ""))
    d = {
        "provider": body.provider.strip() or "custom",
        "host": body.host.strip(),
        "port": body.port,
        "user": body.user.strip(),
        "authcode": authcode,
    }
    ai.CONFIG_DIR.mkdir(exist_ok=True)
    EMAIL_CFG.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    connection_changed = (
        c.get("user") != d.get("user")
        or c.get("host") != d.get("host")
        or int(c.get("port") or 993) != int(d.get("port") or 993)
        or bool(body.clear)
    )
    if connection_changed:
        EMAIL_FETCH_CACHE.unlink(missing_ok=True)
        EMAIL_ANALYSIS_CACHE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/email/states")
def email_message_states():
    account = (_email_cfg().get("user") or "").strip()
    return {"items": db.list_email_message_states(account)}


@app.get("/api/email/preferences")
def email_preferences():
    preferences = _email_preferences()
    return {
        **preferences,
        "seen_ids": None,
        "next_run_at": _next_email_sync_at(preferences),
    }


@app.put("/api/email/preferences")
def save_email_preferences(body: EmailPreferencesIn):
    current = _email_preferences()
    current.update(body.dict())
    _write_email_cache(EMAIL_PREFERENCES, current)
    return {
        **current,
        "seen_ids": None,
        "next_run_at": _next_email_sync_at(current),
    }


@app.post("/api/email/states")
def save_email_message_state(body: EmailMessageStateIn):
    account = (_email_cfg().get("user") or "").strip()
    state = db.save_email_message_state(body.message_key, {
        **body.dict(),
        "account": account,
    })
    db.complete_email_reminder(body.message_key)
    return {"item": state}


@app.delete("/api/email/states/{message_key}")
def restore_email_message(message_key: str):
    if not db.delete_email_message_state(message_key):
        raise HTTPException(404, "邮件处理记录不存在")
    return {"ok": True}


@app.get("/api/email/reminders")
def email_reminders(status: Optional[str] = "todo", limit: int = 20):
    normalized = None if status in (None, "", "all") else status
    if normalized not in (None, "todo", "done"):
        raise HTTPException(422, "不支持的提醒状态")
    return {"items": db.list_email_reminders(normalized, min(max(limit, 1), 100))}


@app.post("/api/email/reminders/{message_key}/complete")
def complete_email_reminder(message_key: str):
    db.complete_email_reminder(message_key)
    return {"ok": True}


def _imap_connect():
    """连接 163/126 IMAP，发 ID 命令，选中 INBOX，返回 (M, imaplib)。调用方负责 M.logout()。"""
    import imaplib
    c = _email_cfg()
    if not c.get("user") or not c.get("authcode"):
        raise HTTPException(400, "还没配置邮箱（去设置填邮箱和 IMAP 授权码）。")
    M = imaplib.IMAP4_SSL(
        c.get("host", "imap.163.com"),
        int(c.get("port") or 993),
        timeout=15,
    )
    M.login(c["user"], c["authcode"])
    try:
        tag = M._new_tag()
        M.send(tag + b' ID ("name" "Foxmail" "version" "7.2" "vendor" "NetEase")\r\n')
        for _ in range(5):
            if M.readline().startswith(tag):
                break
    except Exception:
        pass
    typ, _ = M.select("INBOX")
    if typ != "OK":
        raise HTTPException(400, "SELECT INBOX 失败，请在 163 邮箱设置 → POP3/IMAP 里确认 IMAP 服务已开启")
    return M, imaplib


@app.post("/api/email/test")
def email_test():
    try:
        M, _ = _imap_connect()
        M.logout()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"连接失败：{str(exc)[:160]}")


def _imap_decode(s):
    from email.header import decode_header
    if not s:
        return ""
    out = ""
    for part, enc in decode_header(s):
        out += part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part
    return out


def _imap_body_text(M, num):
    """提取单封邮件的纯文本正文（最多 800 字）。"""
    import email as emaillib
    import re
    try:
        typ, bd = M.fetch(num, "(RFC822)")
        if typ != "OK" or not bd or not bd[0] or not isinstance(bd[0], tuple):
            return ""
        msg = emaillib.message_from_bytes(bd[0][1])
        text = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        text = part.get_payload(decode=True).decode(charset, errors="ignore")
                        break
                    except Exception:
                        pass
        else:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = msg.get_payload(decode=True).decode(charset, errors="ignore")
            except Exception:
                pass
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:800]
    except Exception:
        return ""


@app.get("/api/email/fetch")
def email_fetch(days: int = 30, q: str = "", force: bool = False):
    import email as emaillib
    from email.utils import parsedate_to_datetime
    from datetime import timedelta

    days = max(1, min(int(days or 30), 365))
    cache_key = _email_cache_key(days, q)
    cached = _read_email_cache(EMAIL_FETCH_CACHE)
    cache_entry = (cached.get("entries") or {}).get(cache_key) or {}
    if (
        not force
        and time.time() - float(cache_entry.get("saved_at") or 0) < EMAIL_FETCH_CACHE_SECONDS
        and isinstance(cache_entry.get("result"), dict)
    ):
        return {**cache_entry["result"], "cached": True}

    kws = ["简历", "面试", "笔试", "投递", "录用", "offer", "Offer", "感谢", "邀请", "测评", "申请", "通知", "录取"]
    try:
        M, imaplib = _imap_connect()
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split()[-120:]
        items = []
        for num in reversed(ids):
            typ, d = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = emaillib.message_from_bytes(d[0][1])
            subj = _imap_decode(msg.get("Subject"))
            frm = _imap_decode(msg.get("From"))
            try:
                dt = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = ""
            blob = subj + " " + frm
            if q and q not in blob:
                continue
            job = any(k in blob for k in kws)
            fingerprint = hashlib.sha256(
                f"{dt}\n{frm[:80]}\n{subj}".encode("utf-8")
            ).hexdigest()[:24]
            items.append({
                "id": fingerprint,
                "date": dt,
                "from": frm[:60],
                "subject": subj,
                "job": job,
            })
        M.logout()
        items.sort(key=lambda x: x["date"], reverse=True)
        result = {"items": items, "count": len(items), "cached": False}
        entries = cached.get("entries") or {}
        entries[cache_key] = {"saved_at": time.time(), "result": result}
        if len(entries) > 12:
            entries = dict(sorted(
                entries.items(),
                key=lambda pair: float((pair[1] or {}).get("saved_at") or 0),
                reverse=True,
            )[:12])
        _write_email_cache(EMAIL_FETCH_CACHE, {"entries": entries})
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"拉取失败：{str(e)[:160]}")


def _email_application_candidates(item: dict, applications: list[dict]) -> list[dict]:
    """Build visible choices for review instead of trusting a hidden AI-selected id."""
    company = re.sub(r"[\s（）()·\-—_]+", "", str(item.get("company") or "")).lower()
    suggested_id = item.get("application_id")
    candidates = []
    for application in applications:
        app_company = re.sub(
            r"[\s（）()·\-—_]+", "", str(application.get("company") or "")
        ).lower()
        same_company = bool(
            company and app_company
            and (company in app_company or app_company in company)
        )
        if application.get("id") != suggested_id and not same_company:
            continue
        candidates.append({
            "id": application.get("id"),
            "company": application.get("company") or "",
            "role": application.get("role") or "",
            "status": application.get("status") or "",
            "suggested_by_ai": application.get("id") == suggested_id,
        })
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate["suggested_by_ai"],
            candidate["company"],
            candidate["role"],
        ),
    )


@app.post("/api/email/analyze")
def email_analyze(days: int = 30):
    """拉取近期邮件正文，AI 识别投递阶段，返回 changes 供用户确认后写入看板。"""
    _require_feature_available("email_ai_triage")
    import email as emaillib
    from email.utils import parsedate_to_datetime
    from datetime import timedelta

    days = max(1, min(int(days or 30), 365))
    fetch_cache = _read_email_cache(EMAIL_FETCH_CACHE)
    fetch_entry = (fetch_cache.get("entries") or {}).get(_email_cache_key(days, "")) or {}
    cached_headers = (fetch_entry.get("result") or {}).get("items") or []
    current_ids = sorted(
        item.get("id") for item in cached_headers if item.get("job") and item.get("id")
    )
    analysis_key = hashlib.sha256(
        f"{(_email_cfg().get('user') or '')}|{days}|{'|'.join(current_ids)}".encode("utf-8")
    ).hexdigest()
    cached_analysis = _read_email_cache(EMAIL_ANALYSIS_CACHE)
    if (
        current_ids
        and cached_analysis.get("key") == analysis_key
        and isinstance(cached_analysis.get("result"), dict)
    ):
        handled_keys = {
            item["message_key"]
            for item in db.list_email_message_states((_email_cfg().get("user") or "").strip())
        }
        result = json.loads(json.dumps(cached_analysis["result"]))
        result["items"] = [
            item for item in result.get("items", [])
            if (item.get("email") or {}).get("id") not in handled_keys
        ]
        cached_apps = db.get_applications()
        for item in result["items"]:
            if item.get("type") == "set_application_status":
                suggested_id = item.get("suggested_application_id") or item.get("application_id")
                item["application_id"] = suggested_id
                item["application_candidates"] = _email_application_candidates(item, cached_apps)
                item["suggested_application_id"] = suggested_id
                item["application_id"] = None
                item["requires_application_confirmation"] = True
        result["cached"] = True
        return result

    kws = ["简历", "面试", "笔试", "投递", "录用", "offer", "Offer", "感谢", "邀请", "测评", "申请", "通知", "录取", "拒绝", "遗憾"]
    try:
        M, imaplib = _imap_connect()
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split()[-150:]

        # 第一步：拉所有邮件头，过滤出求职相关的（快）
        job_emails = []
        for num in reversed(ids):
            typ, d = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not d or not d[0]:
                continue
            msg = emaillib.message_from_bytes(d[0][1])
            subj = _imap_decode(msg.get("Subject"))
            frm = _imap_decode(msg.get("From"))
            try:
                dt = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = ""
            if any(k in (subj + frm) for k in kws):
                fingerprint = hashlib.sha256(
                    f"{dt}\n{frm[:80]}\n{subj}".encode("utf-8")
                ).hexdigest()[:24]
                job_emails.append({
                    "_num": num,
                    "id": fingerprint,
                    "date": dt,
                    "from": frm[:80],
                    "subject": subj,
                })

        # 第二步：为求职邮件拉正文（最多 30 封）
        for em in job_emails[:30]:
            em["body"] = _imap_body_text(M, em["_num"])

        M.logout()

        # 清掉内部字段
        for em in job_emails:
            em.pop("_num", None)

        if not job_emails:
            return {"items": [], "summary": f"近 {days} 天没有发现与求职相关的邮件", "email_count": 0}

        # 第三步：AI 识别——每封邮件返回一个 item（含 email_index）
        apps = db.get_applications()
        apps_ctx = [{"id": a["id"], "company": a["company"], "role": a["role"],
                     "status": a["status"], "applied_date": a.get("applied_date", "")}
                    for a in apps]

        system = f"""你是求职管家的邮件智能识别模块。
分析邮件列表，每封求职相关邮件输出一个 item，用于用户确认后写入求职工作台；如邮件包含面试安排，还要同步创建日历事件。

【状态值（只能用以下值）】
- applied   = 已投递（收到投递确认、感谢申请）
- screening = 筛选中（简历审阅中、已查看简历）
- written   = 笔试（收到笔试/在线测评邀请）
- interview = 面试中（收到面试邀请）
- offer     = Offer（录用通知）
- rejected  = 未通过（拒信、遗憾通知）

【已有投递记录】（匹配公司名时参考）
{json.dumps(apps_ctx, ensure_ascii=False, indent=2)}

【规则】
1. 每封求职邮件对应一个 item，email_index 填该邮件的编号（如【3】→ 3）
2. 公司名与已有记录相近 → type = "set_application_status"，填 application_id
3. 全新投递 → type = "create_application"
4. 非求职邮件（广告/系统通知/行程/购物等）→ 直接忽略，不生成 item
5. 同一公司多封邮件只生成最新状态的一个 item
6. 面试、笔试或测评邮件必须尽力抽取 event；没有明确日期时 event 填 null，禁止猜测
7. interview_date 使用 YYYY-MM-DD HH:MM；duration_minutes 默认 60；线上会议链接写 meeting_link，线下地址写 location

【返回格式】严格 JSON，不加 markdown：
{{
  "summary": "一句话总结",
  "items": [
    {{"email_index": 1, "type": "create_application", "company": "字节跳动", "role": "数据产品经理", "status": "applied", "applied_date": "2026-05-21", "reason": "投递确认"}},
    {{"email_index": 3, "type": "set_application_status", "application_id": 2, "company": "美团", "role": "数据分析师", "status": "interview", "reason": "一面邀请", "event": {{"round_type": "一面", "interview_date": "2026-07-30 14:00", "duration_minutes": 60, "location": "", "meeting_link": "https://example.com/meeting"}}}}
  ]
}}"""

        user_msg = f"近 {days} 天的求职相关邮件共 {len(job_emails)} 封：\n\n"
        for i, em in enumerate(job_emails, 1):
            body_preview = em.get("body", "")[:400]
            user_msg += f"【{i}】{em['date']}\n发件人：{em['from']}\n主题：{em['subject']}\n正文：{body_preview}\n\n"

        resp = ai.chat([{"role": "user", "content": user_msg}], system=system,
                       max_tokens=2000, profile_key="fast")
        result = ai.extract_json(resp)
        if not isinstance(result, dict):
            result = {"items": [], "summary": "AI 解析失败，请重试"}

        # 把对应邮件的元数据嵌入每个 item，方便前端展示
        handled_keys = {
            item["message_key"]
            for item in db.list_email_message_states((_email_cfg().get("user") or "").strip())
        }
        items = result.get("items", [])
        apps_by_id = {a["id"]: a for a in apps}
        for item in items:
            idx = item.get("email_index", 0)
            if 1 <= idx <= len(job_emails):
                em = job_emails[idx - 1]
                item["email"] = {
                    "id": em["id"],
                    "date": em["date"],
                    "from": em["from"],
                    "subject": em["subject"],
                }
            existing = apps_by_id.get(item.get("application_id"))
            item["already_current"] = bool(
                existing
                and item.get("type") == "set_application_status"
                and existing.get("status") == item.get("status")
            )
            if item.get("type") == "set_application_status":
                item["application_candidates"] = _email_application_candidates(item, apps)
                item["suggested_application_id"] = item.get("application_id")
                item["application_id"] = None
                item["requires_application_confirmation"] = True
        items = [
            item for item in items
            if (item.get("email") or {}).get("id") not in handled_keys
        ]

        response = {
            "items": items,
            "summary": result.get("summary", ""),
            "email_count": len(job_emails),
            "cached": False,
        }
        resolved_ids = sorted(
            em.get("id") for em in job_emails if em.get("id")
        )
        resolved_key = hashlib.sha256(
            f"{(_email_cfg().get('user') or '')}|{days}|{'|'.join(resolved_ids)}".encode("utf-8")
        ).hexdigest()
        _write_email_cache(EMAIL_ANALYSIS_CACHE, {
            "key": resolved_key,
            "saved_at": time.time(),
            "result": response,
        })
        return response
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"分析失败：{str(e)[:200]}")
