"""Caddie managed AI gateway.

The gateway is intentionally stateless with regard to career data: it proxies
OpenAI-compatible chat requests, stores only coarse usage counters, and never
persists prompts or model responses.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


app = FastAPI(title="Caddie AI Gateway", version="1.0.0")

DATA_DIR = Path(os.environ.get("CADDIE_GATEWAY_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "gateway.sqlite3"
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
MAX_INPUT_BYTES = int(os.environ.get("CADDIE_GATEWAY_MAX_INPUT_BYTES", "200000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("CADDIE_GATEWAY_MAX_OUTPUT_TOKENS", "6000"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("CADDIE_GATEWAY_TIMEOUT_SECONDS", "180"))
DEFAULT_TRIAL_DAYS = int(os.environ.get("CADDIE_TRIAL_DAYS", "14"))
DEFAULT_TOTAL_TOKENS = int(os.environ.get("CADDIE_TRIAL_TOTAL_TOKENS", "2000000"))
DEFAULT_DAILY_TOKENS = int(os.environ.get("CADDIE_TRIAL_DAILY_TOKENS", "300000"))
DEFAULT_DAILY_CALLS = int(os.environ.get("CADDIE_TRIAL_DAILY_CALLS", "30"))
ADMIN_TOKEN = os.environ.get("CADDIE_GATEWAY_ADMIN_TOKEN", "").strip()
WEBSITE_CALLBACK_URL = os.environ.get("CADDIE_WEBSITE_CALLBACK_URL", "").strip()
GATEWAY_CALLBACK_TOKEN = os.environ.get("CADDIE_GATEWAY_CALLBACK_TOKEN", "").strip()
_db_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _tokens() -> dict[str, dict]:
    """Load tester tokens from JSON without ever exposing them in an API."""
    raw = os.environ.get("CADDIE_GATEWAY_TESTERS_JSON", "{}").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CADDIE_GATEWAY_TESTERS_JSON must be valid JSON") from exc
    result: dict[str, dict] = {}
    for tester_id, value in parsed.items():
        if isinstance(value, str):
            value = {"token": value}
        if not isinstance(value, dict) or not value.get("token"):
            continue
        digest = hashlib.sha256(str(value["token"]).encode()).hexdigest()
        result[digest] = {
            "tester_id": str(tester_id)[:80],
            "daily_calls": max(1, int(value.get("daily_calls", 50))),
            "daily_tokens": max(1, int(value.get("daily_tokens", 300000))),
        }
    return result


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_usage (
               tester_id TEXT NOT NULL,
               usage_date TEXT NOT NULL,
               calls INTEGER NOT NULL DEFAULT 0,
               input_tokens INTEGER NOT NULL DEFAULT 0,
               output_tokens INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (tester_id, usage_date)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS invites (
               id TEXT PRIMARY KEY,
               code_hash TEXT UNIQUE NOT NULL,
               batch_name TEXT,
               status TEXT NOT NULL DEFAULT 'ready',
               issued_at TEXT NOT NULL,
               expires_at TEXT NOT NULL,
               trial_days INTEGER NOT NULL,
               total_tokens_limit INTEGER NOT NULL,
               daily_tokens_limit INTEGER NOT NULL,
               daily_calls_limit INTEGER NOT NULL,
               application_public_id TEXT,
               account_id TEXT,
               activated_at TEXT
           )"""
    )
    try:
        conn.execute("ALTER TABLE invites ADD COLUMN application_public_id TEXT")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_invites_application_public_id "
        "ON invites(application_public_id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS accounts (
               id TEXT PRIMARY KEY,
               status TEXT NOT NULL DEFAULT 'active',
               created_at TEXT NOT NULL,
               expires_at TEXT NOT NULL,
               total_tokens_limit INTEGER NOT NULL,
               daily_tokens_limit INTEGER NOT NULL,
               daily_calls_limit INTEGER NOT NULL,
               note TEXT DEFAULT ''
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS devices (
               id TEXT PRIMARY KEY,
               account_id TEXT NOT NULL,
               device_key TEXT NOT NULL,
               device_name TEXT DEFAULT '',
               token_hash TEXT UNIQUE NOT NULL,
               status TEXT NOT NULL DEFAULT 'active',
               created_at TEXT NOT NULL,
               last_seen_at TEXT NOT NULL,
               UNIQUE(account_id, device_key)
           )"""
    )
    conn.commit()
    return conn


def _authenticate(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少内测访问凭证")
    token = authorization.split(" ", 1)[1].strip()
    token_hash = _digest(token)
    legacy = _tokens().get(token_hash)
    if legacy:
        legacy["account_id"] = legacy["tester_id"]
        legacy["total_tokens"] = 0
        legacy["expires_at"] = None
        legacy["status"] = "active"
        return legacy
    conn = _connect()
    row = conn.execute(
        """SELECT d.id AS device_id,d.account_id,d.status AS device_status,
                  a.status,a.expires_at,a.total_tokens_limit AS total_tokens,
                  a.daily_tokens_limit AS daily_tokens,
                  a.daily_calls_limit AS daily_calls
           FROM devices d JOIN accounts a ON a.id=d.account_id
           WHERE d.token_hash=?""",
        (token_hash,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(401, "内测访问凭证无效")
    if row["device_status"] != "active" or row["status"] != "active":
        conn.close()
        raise HTTPException(403, "该内测资格已暂停，请联系测试负责人")
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        conn.close()
        raise HTTPException(403, "本次内测体验已到期")
    conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (_iso(_now()), row["device_id"]))
    conn.commit()
    conn.close()
    return {
        "tester_id": row["account_id"],
        "account_id": row["account_id"],
        "device_id": row["device_id"],
        "daily_calls": int(row["daily_calls"]),
        "daily_tokens": int(row["daily_tokens"]),
        "total_tokens": int(row["total_tokens"]),
        "expires_at": row["expires_at"],
        "status": row["status"],
    }


def _require_admin(authorization: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "管理接口尚未配置")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(401, "管理凭证无效")


def _notify_website_activation(application_public_id: str | None, account_id: str) -> None:
    """Best-effort status callback; activation must survive website downtime."""
    if not application_public_id or not WEBSITE_CALLBACK_URL or not GATEWAY_CALLBACK_TOKEN:
        return
    try:
        requests.post(
            WEBSITE_CALLBACK_URL,
            headers={"Authorization": f"Bearer {GATEWAY_CALLBACK_TOKEN}"},
            json={
                "application_public_id": application_public_id,
                "account_id": account_id,
                "activated_at": _iso(_now()),
            },
            timeout=8,
        ).raise_for_status()
    except requests.RequestException:
        # The admin lookup endpoint supports later reconciliation.
        return


def _all_usage(tester_id: str) -> dict:
    conn = _connect()
    row = conn.execute(
        """SELECT COALESCE(SUM(calls),0) calls,
                  COALESCE(SUM(input_tokens),0) input_tokens,
                  COALESCE(SUM(output_tokens),0) output_tokens
           FROM daily_usage WHERE tester_id=?""",
        (tester_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _usage(tester_id: str) -> dict:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM daily_usage WHERE tester_id=? AND usage_date=?",
        (tester_id, date.today().isoformat()),
    ).fetchone()
    conn.close()
    return dict(row) if row else {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
    }


def _reserve_call(tester: dict) -> None:
    today = date.today().isoformat()
    with _db_lock:
        conn = _connect()
        conn.execute(
            "INSERT OR IGNORE INTO daily_usage(tester_id,usage_date) VALUES(?,?)",
            (tester["tester_id"], today),
        )
        row = conn.execute(
            "SELECT * FROM daily_usage WHERE tester_id=? AND usage_date=?",
            (tester["tester_id"], today),
        ).fetchone()
        total_tokens = int(row["input_tokens"]) + int(row["output_tokens"])
        if int(row["calls"]) >= tester["daily_calls"] or total_tokens >= tester["daily_tokens"]:
            conn.close()
            raise HTTPException(429, "今日内测 AI 额度已用完，请联系测试负责人")
        if tester.get("total_tokens"):
            total = conn.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM daily_usage WHERE tester_id=?",
                (tester["tester_id"],),
            ).fetchone()[0]
            if int(total) >= int(tester["total_tokens"]):
                conn.close()
                raise HTTPException(429, "本次内测 AI 总额度已用完，请联系测试负责人")
        conn.execute(
            "UPDATE daily_usage SET calls=calls+1 WHERE tester_id=? AND usage_date=?",
            (tester["tester_id"], today),
        )
        conn.commit()
        conn.close()


def _record_tokens(tester_id: str, usage: dict) -> None:
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    with _db_lock:
        conn = _connect()
        conn.execute(
            """UPDATE daily_usage
               SET input_tokens=input_tokens+?,output_tokens=output_tokens+?
               WHERE tester_id=? AND usage_date=?""",
            (prompt, completion, tester_id, date.today().isoformat()),
        )
        conn.commit()
        conn.close()


@app.get("/health")
def health():
    return {"ok": True, "service": "caddie-ai-gateway"}


@app.post("/v1/activate")
async def activate(request: Request):
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求格式不是有效 JSON") from exc
    code = str(body.get("invite_code") or "").strip().upper()
    device_key = str(body.get("device_id") or "").strip()[:160]
    device_name = str(body.get("device_name") or "Mac")[:120]
    if len(code) < 8 or not device_key:
        raise HTTPException(400, "请输入有效邀请码")
    with _db_lock:
        conn = _connect()
        invite = conn.execute("SELECT * FROM invites WHERE code_hash=?", (_digest(code),)).fetchone()
        if not invite:
            conn.close()
            raise HTTPException(404, "邀请码不存在")
        if invite["status"] != "ready":
            conn.close()
            raise HTTPException(409, "邀请码已使用或已失效")
        if datetime.fromisoformat(invite["expires_at"]) <= _now():
            conn.execute("UPDATE invites SET status='expired' WHERE id=?", (invite["id"],))
            conn.commit()
            conn.close()
            raise HTTPException(410, "邀请码已过期，请重新申请")
        account_id = "acct_" + uuid.uuid4().hex
        device_id = "dev_" + uuid.uuid4().hex
        credential = "caddie_" + secrets.token_urlsafe(40)
        activated_at = _now()
        account_expires = activated_at + timedelta(days=int(invite["trial_days"]))
        conn.execute(
            "INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?)",
            (
                account_id, "active", _iso(activated_at), _iso(account_expires),
                invite["total_tokens_limit"], invite["daily_tokens_limit"],
                invite["daily_calls_limit"], invite["batch_name"] or "",
            ),
        )
        conn.execute(
            "INSERT INTO devices VALUES(?,?,?,?,?,?,?,?)",
            (
                device_id, account_id, device_key, device_name, _digest(credential),
                "active", _iso(activated_at), _iso(activated_at),
            ),
        )
        conn.execute(
            "UPDATE invites SET status='activated',account_id=?,activated_at=? WHERE id=?",
            (account_id, _iso(activated_at), invite["id"]),
        )
        conn.commit()
        conn.close()
    _notify_website_activation(invite["application_public_id"], account_id)
    return {
        "credential": credential,
        "account_id": account_id,
        "expires_at": _iso(account_expires),
        "total_tokens_limit": int(invite["total_tokens_limit"]),
        "daily_tokens_limit": int(invite["daily_tokens_limit"]),
        "daily_calls_limit": int(invite["daily_calls_limit"]),
    }


@app.post("/admin/v1/invites/batch")
async def create_invite_batch(request: Request, authorization: str | None = Header(None)):
    _require_admin(authorization)
    body = await request.json()
    count = min(100, max(1, int(body.get("count") or 5)))
    valid_hours = min(720, max(1, int(body.get("valid_hours") or 48)))
    batch_name = str(body.get("batch_name") or date.today().isoformat())[:80]
    application_public_id = str(body.get("application_public_id") or "").strip().upper()[:80] or None
    if application_public_id and count != 1:
        raise HTTPException(400, "关联官网申请时 count 必须为 1")
    issued_at = _now()
    codes: list[str] = []
    with _db_lock:
        conn = _connect()
        for _ in range(count):
            code = "-".join((secrets.token_hex(2), secrets.token_hex(2), secrets.token_hex(2))).upper()
            conn.execute(
                """INSERT INTO invites(
                       id,code_hash,batch_name,status,issued_at,expires_at,
                       trial_days,total_tokens_limit,daily_tokens_limit,
                       daily_calls_limit,application_public_id,account_id,activated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "inv_" + uuid.uuid4().hex, _digest(code), batch_name, "ready",
                    _iso(issued_at), _iso(issued_at + timedelta(hours=valid_hours)),
                    int(body.get("trial_days") or DEFAULT_TRIAL_DAYS),
                    int(body.get("total_tokens") or DEFAULT_TOTAL_TOKENS),
                    int(body.get("daily_tokens") or DEFAULT_DAILY_TOKENS),
                    int(body.get("daily_calls") or DEFAULT_DAILY_CALLS),
                    application_public_id, None, None,
                ),
            )
            codes.append(code)
        conn.commit()
        conn.close()
    return {"batch_name": batch_name, "count": len(codes), "invite_codes": codes}


@app.get("/admin/v1/invites/by-application/{application_public_id}")
def invite_by_application(application_public_id: str, authorization: str | None = Header(None)):
    _require_admin(authorization)
    conn = _connect()
    row = conn.execute(
        """SELECT application_public_id,status,issued_at,expires_at,account_id,activated_at
           FROM invites WHERE application_public_id=? ORDER BY issued_at DESC LIMIT 1""",
        (application_public_id.strip().upper(),),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "没有找到对应邀请码")
    return dict(row)


@app.post("/admin/v1/accounts/{account_id}/status")
async def update_account_status(account_id: str, request: Request, authorization: str | None = Header(None)):
    _require_admin(authorization)
    body = await request.json()
    status = str(body.get("status") or "")
    if status not in {"active", "paused", "revoked"}:
        raise HTTPException(400, "status 必须是 active、paused 或 revoked")
    conn = _connect()
    result = conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, account_id))
    conn.commit()
    conn.close()
    if not result.rowcount:
        raise HTTPException(404, "内测账号不存在")
    return {"ok": True, "account_id": account_id, "status": status}


@app.get("/admin/v1/accounts")
def list_accounts(authorization: str | None = Header(None)):
    _require_admin(authorization)
    conn = _connect()
    rows = conn.execute(
        """SELECT a.*,
                  COALESCE(SUM(u.calls),0) calls,
                  COALESCE(SUM(u.input_tokens),0) input_tokens,
                  COALESCE(SUM(u.output_tokens),0) output_tokens,
                  COUNT(DISTINCT d.id) device_count
           FROM accounts a
           LEFT JOIN daily_usage u ON u.tester_id=a.id
           LEFT JOIN devices d ON d.account_id=a.id AND d.status='active'
           GROUP BY a.id ORDER BY a.created_at DESC"""
    ).fetchall()
    conn.close()
    return {"items": [dict(row) for row in rows]}


@app.post("/admin/v1/accounts/{account_id}/quota")
async def update_account_quota(account_id: str, request: Request, authorization: str | None = Header(None)):
    _require_admin(authorization)
    body = await request.json()
    assignments: list[str] = []
    values: list[object] = []
    for field in ("total_tokens_limit", "daily_tokens_limit", "daily_calls_limit"):
        if body.get(field) is not None:
            assignments.append(f"{field}=?")
            values.append(max(1, int(body[field])))
    if body.get("extend_days") is not None:
        conn = _connect()
        current = conn.execute("SELECT expires_at FROM accounts WHERE id=?", (account_id,)).fetchone()
        conn.close()
        if not current:
            raise HTTPException(404, "内测账号不存在")
        assignments.append("expires_at=?")
        values.append(_iso(datetime.fromisoformat(current["expires_at"]) + timedelta(days=max(1, int(body["extend_days"])))))
    if not assignments:
        raise HTTPException(400, "没有需要更新的额度")
    values.append(account_id)
    conn = _connect()
    result = conn.execute(
        f"UPDATE accounts SET {','.join(assignments)} WHERE id=?", tuple(values)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    conn.close()
    if not result.rowcount:
        raise HTTPException(404, "内测账号不存在")
    return dict(row)


@app.get("/v1/models")
def models(authorization: str | None = Header(None)):
    _authenticate(authorization)
    return {"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model"}]}


@app.get("/v1/usage")
def usage(authorization: str | None = Header(None)):
    tester = _authenticate(authorization)
    current = _usage(tester["tester_id"])
    total = _all_usage(tester["tester_id"])
    return {
        **current,
        "daily_calls_limit": tester["daily_calls"],
        "daily_tokens_limit": tester["daily_tokens"],
        "total_usage": total,
        "total_tokens_limit": tester.get("total_tokens") or None,
        "expires_at": tester.get("expires_at"),
        "account_status": tester.get("status", "active"),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    tester = _authenticate(authorization)
    raw = await request.body()
    if len(raw) > MAX_INPUT_BYTES:
        raise HTTPException(413, "本次输入过长，请缩短材料后重试")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "请求格式不是有效 JSON") from exc
    if payload.get("stream"):
        raise HTTPException(400, "当前内测 Gateway 暂不支持流式请求")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages 不能为空")
    payload["model"] = DEFAULT_MODEL
    payload["max_tokens"] = min(
        MAX_OUTPUT_TOKENS,
        max(1, int(payload.get("max_tokens") or MAX_OUTPUT_TOKENS)),
    )
    _reserve_call(tester)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "Gateway 尚未配置模型服务")
    started = time.monotonic()
    try:
        response = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise HTTPException(502, "模型服务暂时不可用，请稍后重试")
    try:
        body = response.json()
    except ValueError:
        raise HTTPException(502, "模型服务返回格式异常")
    if response.status_code >= 400:
        detail = "模型服务暂时不可用"
        if response.status_code == 429:
            detail = "模型服务当前繁忙，请稍后重试"
        elif response.status_code in {401, 403}:
            detail = "Gateway 模型凭证异常，请联系测试负责人"
        return JSONResponse(status_code=response.status_code, content={"error": {"message": detail}})
    _record_tokens(tester["tester_id"], body.get("usage") or {})
    body["caddie_gateway"] = {
        "request_id": uuid.uuid4().hex,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
    return body
