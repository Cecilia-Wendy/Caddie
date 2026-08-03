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
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


app = FastAPI(title="Caddie AI Gateway", version="1.0.0")

DATA_DIR = Path(os.environ.get("CADDIE_GATEWAY_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "gateway.sqlite3"
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
MAX_INPUT_BYTES = int(os.environ.get("CADDIE_GATEWAY_MAX_INPUT_BYTES", "200000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("CADDIE_GATEWAY_MAX_OUTPUT_TOKENS", "6000"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("CADDIE_GATEWAY_TIMEOUT_SECONDS", "180"))
DEFAULT_TRIAL_DAYS = int(os.environ.get("CADDIE_TRIAL_DAYS", "14"))
DEFAULT_TOTAL_TOKENS = int(os.environ.get("CADDIE_TRIAL_TOTAL_TOKENS", "1000000"))
DEFAULT_DAILY_TOKENS = int(os.environ.get("CADDIE_TRIAL_DAILY_TOKENS", "150000"))
DEFAULT_DAILY_CALLS = int(os.environ.get("CADDIE_TRIAL_DAILY_CALLS", "20"))
PRICE_CACHE_HIT_CNY = float(os.environ.get("DEEPSEEK_CACHE_HIT_CNY_PER_M", "0.02"))
PRICE_CACHE_MISS_CNY = float(os.environ.get("DEEPSEEK_CACHE_MISS_CNY_PER_M", "1"))
PRICE_OUTPUT_CNY = float(os.environ.get("DEEPSEEK_OUTPUT_CNY_PER_M", "2"))
ADMIN_TOKEN = os.environ.get("CADDIE_GATEWAY_ADMIN_TOKEN", "").strip()
WEBSITE_CALLBACK_URL = os.environ.get("CADDIE_WEBSITE_CALLBACK_URL", "").strip()
GATEWAY_CALLBACK_TOKEN = os.environ.get("CADDIE_GATEWAY_CALLBACK_TOKEN", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_db_lock = threading.Lock()
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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
               cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
               cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (tester_id, usage_date)
           )"""
    )
    for column in ("cache_hit_tokens", "cache_miss_tokens"):
        try:
            conn.execute(
                f"ALTER TABLE daily_usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
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
    for column, definition in (
        ("email", "TEXT DEFAULT ''"),
        ("email_normalized", "TEXT DEFAULT ''"),
        ("display_name", "TEXT DEFAULT ''"),
        ("cohort_id", "TEXT DEFAULT ''"),
        ("profile_completed", "INTEGER NOT NULL DEFAULT 0"),
    ):
        try:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_email_normalized "
        "ON accounts(email_normalized) WHERE email_normalized<>''"
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
        legacy["device_id"] = "legacy_" + _digest(token)[:20]
        legacy["total_tokens"] = 0
        legacy["expires_at"] = None
        legacy["status"] = "active"
        legacy["email"] = ""
        legacy["display_name"] = legacy["tester_id"]
        legacy["cohort_id"] = "legacy"
        legacy["profile_completed"] = False
        return legacy
    conn = _connect()
    row = conn.execute(
        """SELECT d.id AS device_id,d.account_id,d.status AS device_status,
                  a.status,a.expires_at,a.total_tokens_limit AS total_tokens,
                  a.daily_tokens_limit AS daily_tokens,
                  a.daily_calls_limit AS daily_calls,a.email,a.display_name,
                  a.cohort_id,a.profile_completed
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
        "email": row["email"] or "",
        "display_name": row["display_name"] or "",
        "cohort_id": row["cohort_id"] or "",
        "profile_completed": bool(row["profile_completed"]),
    }


def _validated_profile(body: dict, *, required: bool = True) -> tuple[str, str]:
    email = str(body.get("email") or "").strip().lower()
    display_name = str(body.get("display_name") or "").strip()
    if required and (not email or not display_name):
        raise HTTPException(400, "请填写邮箱和称呼")
    if email and (len(email) > 160 or not EMAIL_RE.match(email)):
        raise HTTPException(400, "请输入有效邮箱")
    if display_name and not 1 <= len(display_name) <= 40:
        raise HTTPException(400, "称呼需为 1-40 个字符")
    return email, display_name


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
                  COALESCE(SUM(output_tokens),0) output_tokens,
                  COALESCE(SUM(cache_hit_tokens),0) cache_hit_tokens,
                  COALESCE(SUM(cache_miss_tokens),0) cache_miss_tokens
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
        "cache_hit_tokens": 0, "cache_miss_tokens": 0,
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
    cache_hit = max(0, int(usage.get("prompt_cache_hit_tokens") or 0))
    cache_miss = max(0, int(usage.get("prompt_cache_miss_tokens") or 0))
    with _db_lock:
        conn = _connect()
        conn.execute(
            """UPDATE daily_usage
               SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,
                   cache_hit_tokens=cache_hit_tokens+?,
                   cache_miss_tokens=cache_miss_tokens+?
               WHERE tester_id=? AND usage_date=?""",
            (prompt, completion, cache_hit, cache_miss, tester_id, date.today().isoformat()),
        )
        conn.commit()
        conn.close()


def _usage_metrics(raw: dict) -> dict:
    result = dict(raw)
    input_tokens = max(0, int(result.get("input_tokens") or 0))
    output_tokens = max(0, int(result.get("output_tokens") or 0))
    cache_hit = max(0, int(result.get("cache_hit_tokens") or 0))
    reported_miss = max(0, int(result.get("cache_miss_tokens") or 0))
    cache_miss = max(reported_miss, input_tokens - cache_hit)
    cache_total = cache_hit + cache_miss
    result["cache_hit_rate"] = round(cache_hit / cache_total, 4) if cache_total else 0.0
    result["estimated_cost_cny"] = round(
        (
            cache_hit * PRICE_CACHE_HIT_CNY
            + cache_miss * PRICE_CACHE_MISS_CNY
            + output_tokens * PRICE_OUTPUT_CNY
        ) / 1_000_000,
        6,
    )
    return result


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
    email, display_name = _validated_profile(body)
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
            """INSERT INTO accounts(
                   id,status,created_at,expires_at,total_tokens_limit,
                   daily_tokens_limit,daily_calls_limit,note,email,
                   email_normalized,display_name,cohort_id,profile_completed
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_id, "active", _iso(activated_at), _iso(account_expires),
                invite["total_tokens_limit"], invite["daily_tokens_limit"],
                invite["daily_calls_limit"], invite["batch_name"] or "",
                email, email, display_name, invite["batch_name"] or "", 1,
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
        "email": email,
        "display_name": display_name,
        "cohort_id": invite["batch_name"] or "",
        "profile_completed": True,
    }


@app.get("/v1/profile")
def profile(authorization: str | None = Header(None)):
    tester = _authenticate(authorization)
    return {key: tester[key] for key in (
        "account_id", "device_id", "email", "display_name", "cohort_id",
        "profile_completed",
    )}


@app.put("/v1/profile")
async def update_profile(request: Request, authorization: str | None = Header(None)):
    tester = _authenticate(authorization)
    email, display_name = _validated_profile(await request.json())
    conn = _connect()
    try:
        conn.execute(
            """UPDATE accounts SET email=?,email_normalized=?,display_name=?,
                      profile_completed=1 WHERE id=?""",
            (email, email, display_name, tester["account_id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "该邮箱已绑定其他内测资格") from exc
    finally:
        conn.close()
    return {"ok": True, "account_id": tester["account_id"], "email": email,
            "display_name": display_name, "profile_completed": True}


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
    current = _usage_metrics(_usage(tester["tester_id"]))
    total = _usage_metrics(_all_usage(tester["tester_id"]))
    return {
        **current,
        "daily_calls_limit": tester["daily_calls"],
        "daily_tokens_limit": tester["daily_tokens"],
        "total_usage": total,
        "total_tokens_limit": tester.get("total_tokens") or None,
        "expires_at": tester.get("expires_at"),
        "account_status": tester.get("status", "active"),
        "email": tester.get("email", ""),
        "display_name": tester.get("display_name", ""),
        "cohort_id": tester.get("cohort_id", ""),
        "profile_completed": bool(tester.get("profile_completed")),
    }


def _supabase_headers() -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(503, "遥测云端尚未配置")
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


def _analytics_events(days: int) -> list[dict]:
    days = max(1, min(int(days), 90))
    since = _iso(_now() - timedelta(days=days))
    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/telemetry_events",
            headers=_supabase_headers(),
            params={
                "select": "event_name,account_id,properties,client_time,app_version",
                "client_time": f"gte.{since}",
                "order": "client_time.desc",
                "limit": "10000",
            },
            timeout=(8, 30),
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise HTTPException(502, "暂时无法读取产品分析数据") from exc


@app.get("/admin/v1/analytics/overview")
def analytics_overview(days: int = 14, authorization: str | None = Header(None)):
    _require_admin(authorization)
    events = _analytics_events(days)
    conn = _connect()
    accounts = [dict(row) for row in conn.execute(
        "SELECT accounts.id,email,display_name,cohort_id,status,created_at,last_seen_at "
        "FROM accounts LEFT JOIN (SELECT account_id,MAX(last_seen_at) last_seen_at "
        "FROM devices GROUP BY account_id) d ON d.account_id=accounts.id "
        "ORDER BY created_at DESC"
    ).fetchall()]
    conn.close()
    by_user: dict[str, dict] = {
        account["id"]: {**account, "events": 0, "last_event_at": None, "features": {}}
        for account in accounts
    }
    event_counts: dict[str, int] = {}
    active_days: dict[str, set[str]] = {}
    for event in events:
        name = str(event.get("event_name") or "unknown")
        event_counts[name] = event_counts.get(name, 0) + 1
        account_id = str(event.get("account_id") or "")
        if account_id in by_user:
            item = by_user[account_id]
            item["events"] += 1
            item["last_event_at"] = item["last_event_at"] or event.get("client_time")
            props = event.get("properties") or {}
            feature = str(props.get("feature") or name)
            item["features"][feature] = item["features"].get(feature, 0) + 1
            active_days.setdefault(account_id, set()).add(str(event.get("client_time") or "")[:10])
    for account_id, item in by_user.items():
        item["active_days"] = len(active_days.get(account_id, set()))
    return {
        "days": max(1, min(int(days), 90)),
        "total_users": len(accounts),
        "active_users": sum(1 for item in by_user.values() if item["events"]),
        "total_events": len(events),
        "event_counts": event_counts,
        "users": list(by_user.values()),
    }


ADMIN_DASHBOARD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Caddie 内测数据</title>
<style>body{margin:0;font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#172033;background:#f5f7fa}main{max-width:1180px;margin:0 auto;padding:40px 24px}header{display:flex;justify-content:space-between;align-items:end;margin-bottom:24px}h1{margin:0;font-size:28px}p{color:#718096}.login,.panel{background:white;border:1px solid #dfe5ec;border-radius:8px;padding:20px}.login{display:flex;gap:10px}input,button,select{font:inherit;padding:10px 12px;border:1px solid #ccd5e0;border-radius:6px}input{flex:1}button{background:#2563eb;color:white;border-color:#2563eb;cursor:pointer}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}.stat{background:white;border:1px solid #dfe5ec;padding:18px;border-radius:8px}.stat b{display:block;font-size:26px;margin-top:8px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #e7ebf0}th{color:#718096;font-size:12px}.muted{color:#8a96a6}.tag{display:inline-block;background:#eef4ff;color:#245ac7;padding:3px 7px;border-radius:5px;margin:2px}#content{display:none}@media(max-width:720px){.stats{grid-template-columns:1fr}.login{display:block}.login>*{box-sizing:border-box;width:100%;margin:4px 0}}</style></head>
<body><main><header><div><h1>Caddie 内测数据</h1><p>按用户查看活跃、功能使用与反馈线索，不展示求职资料或 AI 正文。</p></div><select id="days"><option value="7">7 天</option><option value="14" selected>14 天</option><option value="30">30 天</option></select></header><section class="login" id="login"><input id="token" type="password" placeholder="Gateway 管理 Token"><button onclick="load()">打开数据台</button></section><div id="content"><section class="stats"><div class="stat">内测用户<b id="users">0</b></div><div class="stat">活跃用户<b id="active">0</b></div><div class="stat">行为事件<b id="events">0</b></div></section><section class="panel"><table><thead><tr><th>用户</th><th>批次</th><th>活跃天数</th><th>事件</th><th>主要使用</th><th>最后活跃</th></tr></thead><tbody id="rows"></tbody></table></section></div></main>
<script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function load(){const token=document.querySelector('#token').value||sessionStorage.caddieAdminToken;if(!token)return;sessionStorage.caddieAdminToken=token;const days=document.querySelector('#days').value;const r=await fetch('/admin/v1/analytics/overview?days='+days,{headers:{Authorization:'Bearer '+token}});if(!r.ok){alert((await r.json()).detail||'读取失败');return}const d=await r.json();login.style.display='none';content.style.display='block';users.textContent=d.total_users;active.textContent=d.active_users;events.textContent=d.total_events;rows.innerHTML=d.users.map(u=>'<tr><td><b>'+esc(u.display_name||'待补充')+'</b><br><span class="muted">'+esc(u.email||u.id)+'</span></td><td>'+esc(u.cohort_id||'—')+'</td><td>'+u.active_days+'</td><td>'+u.events+'</td><td>'+Object.entries(u.features).sort((a,b)=>b[1]-a[1]).slice(0,4).map(x=>'<span class="tag">'+esc(x[0])+' '+x[1]+'</span>').join('')+'</td><td>'+esc((u.last_event_at||u.last_seen_at||'—').slice(0,19))+'</td></tr>').join('')}days.onchange=load;if(sessionStorage.caddieAdminToken)load();</script></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    return HTMLResponse(ADMIN_DASHBOARD, headers={"Cache-Control": "no-store"})


@app.post("/v1/telemetry/events")
async def ingest_telemetry(request: Request, authorization: str | None = Header(None)):
    tester = _authenticate(authorization)
    body = await request.json()
    events = body if isinstance(body, list) else body.get("events")
    if not isinstance(events, list) or not events or len(events) > 100:
        raise HTTPException(400, "events 必须是 1-100 条事件")
    allowed = {
        "event_id", "event_name", "schema_version", "installation_id", "session_id",
        "entity_type", "entity_id_hash", "properties", "client_time", "app_version", "platform",
    }
    cleaned = []
    for event in events:
        if not isinstance(event, dict):
            raise HTTPException(400, "事件格式无效")
        item = {key: event.get(key) for key in allowed if key in event}
        item.update({
            "account_id": tester["account_id"],
            "device_id": tester["device_id"],
            "cohort_id": tester.get("cohort_id") or "",
        })
        cleaned.append(item)
    try:
        response = requests.post(
            f"{SUPABASE_URL}/rest/v1/telemetry_events",
            headers={**_supabase_headers(), "Prefer": "return=minimal"},
            json=cleaned,
            timeout=(8, 30),
        )
        if response.status_code not in {200, 201, 204, 409}:
            response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(502, "遥测写入失败") from exc
    return {"ok": True, "accepted": len(cleaned)}


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
    # Use an opaque stable identifier so DeepSeek isolates KV cache per tester
    # without receiving an email, device identifier, or other personal data.
    payload["user_id"] = "caddie_" + _digest(tester["tester_id"])[:32]
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
