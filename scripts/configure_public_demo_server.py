#!/usr/bin/env python3
"""Prepare the server-side state for the isolated public Caddie demo.

This script intentionally never prints or returns the generated gateway token.
It is meant to run as root on the demo server.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path


def atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(raw, mode)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def sqlite_snapshot(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    replacement = target.with_suffix(".building")
    replacement.unlink(missing_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(replacement)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(replacement, 0o600)
    os.replace(replacement, target)


def update_gateway_env(path: Path, token: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    key = "CADDIE_GATEWAY_TESTERS_JSON="
    found = False
    output: list[str] = []
    for line in lines:
        if not line.startswith(key):
            output.append(line)
            continue
        found = True
        try:
            testers = json.loads(line[len(key):])
        except json.JSONDecodeError:
            testers = {}
        testers["demo-web"] = {
            "token": token,
            "daily_calls": 25,
            "daily_tokens": 120_000,
        }
        output.append(key + json.dumps(testers, ensure_ascii=False, separators=(",", ":")))
    if not found:
        output.append(key + json.dumps({
            "demo-web": {
                "token": token,
                "daily_calls": 25,
                "daily_tokens": 120_000,
            }
        }, ensure_ascii=False, separators=(",", ":")))
    atomic_text(path, "\n".join(output) + "\n")


def write_demo_ai_config(path: Path, token: str) -> None:
    provider_id = "caddie-public-demo"
    provider = {
        "id": provider_id,
        "name": "Caddie Demo AI",
        "type": "openai",
        "base_url": "http://caddie-gateway:8080/v1",
        "api_key": token,
        "model": "deepseek-chat",
        "capabilities": [
            "text", "reasoning", "writing", "structured_output", "long_context"
        ],
        "data_policy": "domestic",
    }
    config = {
        "providers": [provider],
        "active_provider_id": provider_id,
        "model_profiles": {
            "deep_reasoning": [provider_id],
            "research": [provider_id],
            "writing": [provider_id],
            "fast": [provider_id],
        },
    }
    atomic_text(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/opt/caddie-data")
    parser.add_argument("--gateway-env", default="/opt/caddie-gateway/gateway.env")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    gateway_env = Path(args.gateway_env)
    token_file = data_dir / "demo-gateway-token"
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
    else:
        token = secrets.token_urlsafe(36)
        atomic_text(token_file, token + "\n")

    update_gateway_env(gateway_env, token)
    write_demo_ai_config(data_dir / "config.json", token)
    sqlite_snapshot(data_dir / "caddie.db", data_dir / "demo-template" / "caddie.db")
    (data_dir / "demo-sessions").mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir / "demo-sessions", 0o700)
    print("PUBLIC_DEMO_STATE_READY")
    print("Gateway credential: dedicated demo-web token (not printed)")
    print("Quota: 25 calls/day, 120000 tokens/day")


if __name__ == "__main__":
    main()
