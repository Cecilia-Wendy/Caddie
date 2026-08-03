"""Hosted Alpha activation and macOS Keychain credential storage."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import requests


DATA_DIR = Path(os.environ.get("CADDIE_DATA_DIR") or (Path.home() / ".caddie"))
STATE_PATH = DATA_DIR / "hosted_access.json"
KEYCHAIN_SERVICE = "app.caddie.gateway"
KEYCHAIN_MARKER = "__caddie_keychain__"
DEFAULT_BASE_URL = "https://43-128-7-135.sslip.io/gateway/v1"


class HostedAccessError(RuntimeError):
    pass


def _read_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(value: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(STATE_PATH, 0o600)


def device_id() -> str:
    state = _read_state()
    value = str(state.get("device_id") or "")
    if not value:
        value = "mac_" + uuid.uuid4().hex
        state["device_id"] = value
        _write_state(state)
    return value


def _security(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("/usr/bin/security", *args), text=True, capture_output=True, check=False
    )


def store_credential(credential: str) -> None:
    if not credential:
        raise HostedAccessError("服务端没有返回设备凭证")
    result = _security(
        "add-generic-password", "-U", "-a", device_id(), "-s", KEYCHAIN_SERVICE,
        "-w", credential,
    )
    if result.returncode:
        raise HostedAccessError("无法将内测凭证保存到 macOS 钥匙串")


def credential() -> str:
    override = os.environ.get("CADDIE_HOSTED_CREDENTIAL", "").strip()
    if override:
        return override
    result = _security(
        "find-generic-password", "-a", device_id(), "-s", KEYCHAIN_SERVICE, "-w"
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def clear_credential() -> None:
    _security("delete-generic-password", "-a", device_id(), "-s", KEYCHAIN_SERVICE)


def activate(invite_code: str, base_url: str = DEFAULT_BASE_URL) -> dict:
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/activate",
            json={
                "invite_code": invite_code.strip(),
                "device_id": device_id(),
                "device_name": "Mac",
            },
            timeout=20,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise HostedAccessError("暂时无法连接 Caddie 内测服务") from exc
    if response.status_code >= 400:
        raise HostedAccessError(str(payload.get("detail") or "邀请码激活失败"))
    store_credential(str(payload.pop("credential", "")))
    state = _read_state()
    state.update({
        "device_id": device_id(),
        "account_id": payload.get("account_id"),
        "expires_at": payload.get("expires_at"),
        "base_url": base_url.rstrip("/"),
        "activated": True,
    })
    _write_state(state)
    return payload


def quota() -> dict:
    token = credential()
    state = _read_state()
    if not token:
        return {"activated": False, **state}
    try:
        response = requests.get(
            f"{str(state.get('base_url') or DEFAULT_BASE_URL).rstrip('/')}/usage",
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        return {"activated": True, "reachable": False, **state}
    if response.status_code >= 400:
        return {
            "activated": True, "reachable": True, "valid": False,
            "message": payload.get("detail") or "凭证不可用", **state,
        }
    return {"activated": True, "reachable": True, "valid": True, **state, **payload}
