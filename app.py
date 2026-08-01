"""Caddie Hosted Alpha AI gateway.

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
from datetime import date
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


app = FastAPI(title="Caddie Hosted Alpha Gateway", version="0.2.0")

DATA_DIR = Path(os.environ.get("CADDIE_GATEWAY_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "gateway.sqlite3"
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
MAX_INPUT_BYTES = int(os.environ.get("CADDIE_GATEWAY_MAX_INPUT_BYTES", "200000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("CADDIE_GATEWAY_MAX_OUTPUT_TOKENS", "6000"))
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("CADDIE_GATEWAY_TIMEOUT_SECONDS", "180"))
_db_lock = threading.Lock()


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
    return conn


def _authenticate(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "缺少内测访问凭证")
    token = authorization.split(" ", 1)[1].strip()
    tester = _tokens().get(hashlib.sha256(token.encode()).hexdigest())
    if not tester:
        raise HTTPException(401, "内测访问凭证无效")
    return tester


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
    return {"ok": True, "service": "caddie-hosted-alpha-gateway"}


@app.get("/v1/models")
def models(authorization: str | None = Header(None)):
    _authenticate(authorization)
    return {"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model"}]}


@app.get("/v1/usage")
def usage(authorization: str | None = Header(None)):
    tester = _authenticate(authorization)
    current = _usage(tester["tester_id"])
    return {
        **current,
        "daily_calls_limit": tester["daily_calls"],
        "daily_tokens_limit": tester["daily_tokens"],
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
