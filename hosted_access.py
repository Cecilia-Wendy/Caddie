"""Hosted Alpha activation and macOS Keychain credential storage."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import uuid
import base64
import ctypes
from pathlib import Path

import requests


DATA_DIR = Path(os.environ.get("CADDIE_DATA_DIR") or (Path.home() / ".caddie"))
STATE_PATH = DATA_DIR / "hosted_access.json"
KEYCHAIN_SERVICE = "app.caddie.gateway"
KEYCHAIN_MARKER = "__caddie_keychain__"
WINDOWS_CREDENTIAL_KEY = "credential_dpapi"
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
        prefix = "win" if os.name == "nt" else "mac" if platform.system() == "Darwin" else "device"
        value = prefix + "_" + uuid.uuid4().hex
        state["device_id"] = value
        _write_state(state)
    return value


def _security(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ("/usr/bin/security", *args), text=True, capture_output=True, check=False
    )


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_protect(value: str) -> str:
    raw = value.encode("utf-8")
    source_buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    protected = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "Caddie", None, None, None, 0,
        ctypes.byref(protected),
    ):
        raise HostedAccessError("Windows 无法加密内测凭证")
    try:
        data = ctypes.string_at(protected.pbData, protected.cbData)
        return base64.b64encode(data).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(protected.pbData)


def _windows_unprotect(value: str) -> str:
    if not value:
        return ""
    try:
        raw = base64.b64decode(value)
        source_buffer = ctypes.create_string_buffer(raw)
        source = _DataBlob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        plain = _DataBlob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0,
            ctypes.byref(plain),
        ):
            return ""
        try:
            return ctypes.string_at(plain.pbData, plain.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(plain.pbData)
    except (ValueError, OSError):
        return ""


def store_credential(credential: str) -> None:
    if not credential:
        raise HostedAccessError("服务端没有返回设备凭证")
    if os.name == "nt":
        state = _read_state()
        state[WINDOWS_CREDENTIAL_KEY] = _windows_protect(credential)
        _write_state(state)
        return
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
    if os.name == "nt":
        return _windows_unprotect(str(_read_state().get(WINDOWS_CREDENTIAL_KEY) or ""))
    result = _security(
        "find-generic-password", "-a", device_id(), "-s", KEYCHAIN_SERVICE, "-w"
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def clear_credential() -> None:
    if os.name == "nt":
        state = _read_state()
        state.pop(WINDOWS_CREDENTIAL_KEY, None)
        _write_state(state)
        return
    _security("delete-generic-password", "-a", device_id(), "-s", KEYCHAIN_SERVICE)


def activate(invite_code: str, base_url: str = DEFAULT_BASE_URL) -> dict:
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/activate",
            json={
                "invite_code": invite_code.strip(),
                "device_id": device_id(),
                "device_name": platform.system() or "Desktop",
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
