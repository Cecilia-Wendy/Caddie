"""Privacy-first product telemetry for the Caddie Alpha.

Events are queued locally before a background worker uploads registered,
coarse-grained metadata. Career content, prompts, file paths and personal
identifiers are rejected.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import db
import requests
from app_version import APP_VERSION


CONSENT_VERSION = "2026-07-essential-v1"

EVENT_SCHEMAS: dict[str, set[str]] = {
    "analytics_consent_updated": {"enabled", "consent_version"},
    "onboarding_started": {"entry_point"},
    "onboarding_completed": {"import_method", "ai_mode", "ai_configured"},
    "product_tour_completed": {"version"},
    "view_opened": {"view_name"},
    "source_added": {"source_type", "origin", "size_bucket"},
    "source_ingested": {"source_type", "has_track_link"},
    "job_track_created": {"has_jd", "creation_source"},
    "asset_generated": {"asset_type", "entry_point", "status"},
    "asset_saved": {"asset_type", "was_edited"},
    "feedback_submitted": {"category", "rating_bucket"},
    "proposed_change_accepted": {"target_type", "action_type"},
    "proposed_change_rejected": {"target_type", "action_type"},
    "app_started": {"launch_mode", "os_family", "version_changed"},
    "feature_action_completed": {
        "feature", "action", "status", "duration_bucket", "http_status",
    },
    "ai_call_completed": {
        "profile", "status", "latency_bucket", "input_tokens_bucket",
        "output_tokens_bucket", "fallback_used", "retry_count", "error_type",
        "provider_type", "model_family",
    },
    "external_agent_tool_completed": {
        "client_type", "agent_family", "tool_name", "operation_class",
        "status", "duration_bucket", "result_count_bucket", "error_type",
    },
}

SENSITIVE_KEYS = {
    "content", "body", "document", "prompt", "completion", "message", "detail",
    "name", "email", "phone", "path", "filename", "api_key", "token", "password",
    "company", "role", "jd", "feedback_text",
}
SAFE_VALUE = re.compile(r"^[a-zA-Z0-9_.:+-]{0,80}$")
UPLOAD_BATCH_SIZE = 50
UPLOAD_INTERVAL_SECONDS = 300
UPLOAD_DEBOUNCE_SECONDS = 2
_uploader_started = False
_uploader_lock = threading.Lock()
_upload_wakeup = threading.Event()


def duration_bucket(milliseconds: int | float | None) -> str:
    value = max(0, int(milliseconds or 0))
    if value < 500:
        return "under_500ms"
    if value < 2_000:
        return "500ms_2s"
    if value < 10_000:
        return "2s_10s"
    if value < 30_000:
        return "10s_30s"
    if value < 120_000:
        return "30s_2m"
    return "2m_plus"


def token_bucket(value: int | float | None) -> str:
    count = max(0, int(value or 0))
    if count == 0:
        return "none"
    if count < 1_000:
        return "under_1k"
    if count < 4_000:
        return "1k_4k"
    if count < 16_000:
        return "4k_16k"
    if count < 64_000:
        return "16k_64k"
    return "64k_plus"


def count_bucket(value: int | float | None) -> str:
    count = max(0, int(value or 0))
    if count == 0:
        return "none"
    if count == 1:
        return "one"
    if count <= 5:
        return "2_5"
    if count <= 20:
        return "6_20"
    return "20_plus"


def classify_ai_error(message: str) -> str:
    lowered = (message or "").lower()
    if any(key in lowered for key in ("429", "rate", "频繁")):
        return "rate_limit"
    if any(key in lowered for key in ("401", "403", "api key", "鉴权")):
        return "auth"
    if any(key in lowered for key in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(key in lowered for key in ("json", "格式", "解析", "空内容", "最终回复")):
        return "invalid_output"
    if any(key in lowered for key in ("quota", "billing", "额度", "余额")):
        return "quota"
    if any(key in lowered for key in ("connect", "network", "网络")):
        return "network"
    return "provider_error" if message else "none"


def _resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def _upload_config() -> dict:
    path = _resource_path("assets", "telemetry_channel.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _settings(create: bool = True) -> dict:
    conn = db.get_db()
    row = conn.execute("SELECT * FROM telemetry_settings WHERE id=1").fetchone()
    if not row and create:
        installation_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO telemetry_settings
               (id,enabled,consent_version,consented_at,installation_id)
               VALUES(1,1,?,?,?)""",
            (CONSENT_VERSION, _now(), installation_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM telemetry_settings WHERE id=1").fetchone()
    result = dict(row) if row else {}
    conn.close()
    return result


def get_settings() -> dict:
    current = _settings()
    conn = db.get_db()
    counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status,COUNT(*) n FROM telemetry_queue GROUP BY status"
        ).fetchall()
    }
    conn.close()
    return {
        "enabled": bool(current.get("enabled")),
        "consent_version": current.get("consent_version"),
        "consented_at": current.get("consented_at"),
        "mode": "anonymous_cloud" if _upload_config().get("project_url") else "local_only",
        "pending_count": counts.get("pending", 0) + counts.get("local", 0),
        "local_count": sum(counts.values()),
        "uploaded_count": counts.get("uploaded", 0),
        "last_upload_at": current.get("last_upload_at"),
        "last_upload_status": current.get("last_upload_status"),
        "app_version": APP_VERSION,
    }


def ensure_required_analytics() -> dict:
    _settings()
    conn = db.get_db()
    conn.execute(
        """UPDATE telemetry_settings
           SET enabled=?,consent_version=?,consented_at=?,updated_at=datetime('now','localtime')
           WHERE id=1""",
        (1, CONSENT_VERSION, _now()),
    )
    conn.commit()
    conn.close()
    return get_settings()


def set_consent(enabled: bool, consent_version: str = CONSENT_VERSION) -> dict:
    # Kept for API compatibility. Essential anonymous product telemetry cannot
    # be disabled; the endpoint always restores the required setting.
    before = _settings()
    result = ensure_required_analytics()
    if not before.get("enabled"):
        log_event(
            "analytics_consent_updated",
            {"enabled": True, "consent_version": CONSENT_VERSION},
            force=True,
        )
    return result


def _safe_properties(event_name: str, properties: dict[str, Any] | None) -> dict:
    if event_name not in EVENT_SCHEMAS:
        raise ValueError(f"unregistered telemetry event: {event_name}")
    allowed = EVENT_SCHEMAS[event_name]
    clean: dict[str, Any] = {}
    for key, value in (properties or {}).items():
        if key in SENSITIVE_KEYS or key not in allowed:
            continue
        if value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        elif isinstance(value, str) and SAFE_VALUE.fullmatch(value):
            clean[key] = value
    return clean


def _entity_hash(installation_id: str, entity_type: str | None, entity_id: Any) -> str | None:
    if not entity_type or entity_id is None:
        return None
    payload = f"{entity_type}:{entity_id}".encode()
    return hmac.new(installation_id.encode(), payload, hashlib.sha256).hexdigest()


def log_event(
    event_name: str,
    properties: dict[str, Any] | None = None,
    *,
    entity_type: str | None = None,
    entity_id: Any = None,
    session_id: str | None = None,
    force: bool = False,
) -> str | None:
    clean = _safe_properties(event_name, properties)
    clean_session_id = (
        session_id if isinstance(session_id, str) and SAFE_VALUE.fullmatch(session_id) else None
    )
    settings = _settings()
    if not force and not settings.get("enabled"):
        return None
    event_id = str(uuid.uuid4())
    conn = None
    try:
        conn = db.get_db()
        conn.execute(
            """INSERT INTO telemetry_queue
               (event_id,event_name,installation_id,session_id,entity_type,entity_id_hash,
                properties_json,client_time,status)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                event_name,
                settings["installation_id"],
                clean_session_id,
                entity_type,
                _entity_hash(settings["installation_id"], entity_type, entity_id),
                json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
                _now(),
                "local",
            ),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        # Analytics is deliberately best-effort. A busy local database must
        # never break chat, interview parsing, or another user action.
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return None
        raise
    finally:
        if conn is not None:
            conn.close()
    # Wake the single background worker instead of creating a network thread
    # for every product action.
    _upload_wakeup.set()
    return event_id


def list_events(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    conn = db.get_db()
    rows = conn.execute(
        """SELECT event_id,event_name,properties_json,client_time,status
           FROM telemetry_queue ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            **{k: row[k] for k in ("event_id", "event_name", "client_time", "status")},
            "properties": json.loads(row["properties_json"] or "{}"),
        }
        for row in rows
    ]


def clear_events() -> int:
    conn = db.get_db()
    cur = conn.execute("DELETE FROM telemetry_queue")
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def local_summary(days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    conn = db.get_db()
    rows = conn.execute(
        """SELECT event_name,COUNT(*) n,MAX(client_time) last_seen
           FROM telemetry_queue
           WHERE client_time >= datetime('now', ?)
           GROUP BY event_name ORDER BY n DESC,event_name""",
        (f"-{days} days",),
    ).fetchall()
    conn.close()
    return {
        "days": days,
        "mode": "local_only",
        "events": [dict(row) for row in rows],
        "total": sum(row["n"] for row in rows),
    }


def _pending_rows(limit: int = UPLOAD_BATCH_SIZE) -> list:
    conn = db.get_db()
    rows = conn.execute(
        """SELECT event_id,event_name,schema_version,installation_id,session_id,
                  entity_type,entity_id_hash,properties_json,client_time
           FROM telemetry_queue
           WHERE status IN ('local','pending')
           ORDER BY id LIMIT ?""",
        (max(1, min(int(limit), UPLOAD_BATCH_SIZE)),),
    ).fetchall()
    conn.close()
    return rows


def _cloud_event(row) -> dict:
    return {
        "event_id": row["event_id"],
        "event_name": row["event_name"],
        "schema_version": row["schema_version"],
        "installation_id": row["installation_id"],
        "session_id": row["session_id"],
        "entity_type": row["entity_type"],
        "entity_id_hash": row["entity_id_hash"],
        "properties": json.loads(row["properties_json"] or "{}"),
        "client_time": row["client_time"],
        "app_version": APP_VERSION,
        "platform": "macos",
    }


def _record_upload_status(status: str):
    conn = db.get_db()
    conn.execute(
        """UPDATE telemetry_settings
           SET last_upload_at=?,last_upload_status=?,updated_at=datetime('now','localtime')
           WHERE id=1""",
        (_now(), status[:200]),
    )
    conn.commit()
    conn.close()


def upload_pending() -> dict:
    settings = ensure_required_analytics()
    config = _upload_config()
    project_url = str(config.get("project_url") or "").rstrip("/")
    publishable_key = str(config.get("publishable_key") or "")
    if not project_url or not publishable_key:
        return {"ok": True, "status": "not_configured", "uploaded": 0}

    rows = _pending_rows()
    if not rows:
        _record_upload_status("current")
        return {"ok": True, "status": "current", "uploaded": 0}

    event_ids = [row["event_id"] for row in rows]
    payload = [_cloud_event(row) for row in rows]
    endpoint = f"{project_url}/rest/v1/telemetry_events"
    headers = {
        "apikey": publishable_key,
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    delivered_ids: list[str] = []
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=(8, 30),
        )
        if response.status_code == 409:
            # A previous timed-out request may already have committed part or
            # all of this batch. Resolve duplicates one event at a time without
            # granting anonymous clients any SELECT permission.
            for event in payload:
                item_response = requests.post(
                    endpoint,
                    headers=headers,
                    json=[event],
                    timeout=(8, 20),
                )
                if item_response.status_code in {200, 201, 204, 409}:
                    delivered_ids.append(event["event_id"])
                else:
                    item_response.raise_for_status()
        else:
            response.raise_for_status()
            delivered_ids = event_ids
    except requests.RequestException as exc:
        response_detail = ""
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                response_detail = (response.text or "").strip()[:500]
            except Exception:
                response_detail = ""
        error_message = str(exc)
        if response_detail:
            error_message = f"{error_message} | Supabase: {response_detail}"
        conn = db.get_db()
        conn.executemany(
            "UPDATE telemetry_queue SET status='pending' WHERE event_id=?",
            [(event_id,) for event_id in event_ids if event_id not in delivered_ids],
        )
        conn.executemany(
            "UPDATE telemetry_queue SET status='uploaded' WHERE event_id=?",
            [(event_id,) for event_id in delivered_ids],
        )
        conn.commit()
        conn.close()
        _record_upload_status(f"failed: {error_message[:160]}")
        return {
            "ok": False,
            "status": "failed",
            "uploaded": len(delivered_ids),
            "error": error_message,
        }

    conn = db.get_db()
    conn.executemany(
        "UPDATE telemetry_queue SET status='uploaded' WHERE event_id=?",
        [(event_id,) for event_id in delivered_ids],
    )
    conn.commit()
    conn.close()
    _record_upload_status("success")
    return {"ok": True, "status": "success", "uploaded": len(delivered_ids)}


def _uploader_loop():
    time.sleep(2)
    while True:
        # Coalesce a short burst of related UI events into one cloud request.
        _upload_wakeup.clear()
        time.sleep(UPLOAD_DEBOUNCE_SECONDS)
        try:
            upload_pending()
        except Exception:
            # Product telemetry must never interrupt the local application.
            pass
        # New events upload promptly; the timeout is a recovery sweep for
        # offline or previously failed batches.
        _upload_wakeup.wait(UPLOAD_INTERVAL_SECONDS)


def start_uploader():
    global _uploader_started
    if os.environ.get("CADDIE_DISABLE_TELEMETRY_UPLOAD") == "1":
        return
    with _uploader_lock:
        if _uploader_started:
            return
        _uploader_started = True
        threading.Thread(target=_uploader_loop, daemon=True, name="caddie-telemetry").start()
