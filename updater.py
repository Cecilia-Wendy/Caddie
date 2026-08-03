"""Signed, local-first update support for Caddie Alpha.

The first installation still requires explicit macOS trust. Later releases are
accepted only when their manifest and package match Caddie's embedded Ed25519
public key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from app_version import APP_VERSION, UPDATE_MANIFEST_SCHEMA


DATA_DIR = Path(os.environ.get("CADDIE_DATA_DIR") or (Path.home() / ".caddie"))
UPDATE_DIR = DATA_DIR / "updates"
STATE_PATH = UPDATE_DIR / "state.json"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 500 * 1024 * 1024
SIGNED_FIELDS = (
    "schema_version",
    "version",
    "published_at",
    "download_url",
    "sha256",
    "arch",
    "bundle_id",
)


class UpdateError(RuntimeError):
    pass


def _resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def _read_json(path: Path, fallback: dict | None = None) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (fallback or {})
    except (OSError, json.JSONDecodeError):
        return fallback or {}


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _state(**patch) -> dict:
    value = _read_json(STATE_PATH, {})
    value.update(patch)
    value["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(STATE_PATH, value)
    return value


def current_state() -> dict:
    channel = _read_json(_resource_path("assets", "update_channel.json"), {})
    state = _read_json(STATE_PATH, {})
    manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else {}
    if (
        state.get("status") == "applying"
        and manifest.get("version") == APP_VERSION
    ):
        state = _state(
            status="current",
            installed_version=APP_VERSION,
            installed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            error=None,
        )
    return {
        "current_version": APP_VERSION,
        "channel": channel.get("channel") or "alpha",
        "manifest_url": os.environ.get("CADDIE_UPDATE_MANIFEST_URL")
        or channel.get("manifest_url")
        or "",
        "packaged": bool(getattr(sys, "frozen", False)),
        "state": state,
    }


def _version_key(value: str) -> tuple:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", value.strip())
    if not match:
        raise UpdateError(f"无法识别版本号：{value}")
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    suffix = match.group(4)
    if not suffix:
        return major, minor, patch, 4, 0, ""
    name, _, number = suffix.partition(".")
    rank = {"alpha": 0, "beta": 1, "rc": 2}.get(name.lower(), 0)
    return major, minor, patch, rank, int(number or 0) if (number or "0").isdigit() else 0, suffix


def is_newer(version: str) -> bool:
    return _version_key(version) > _version_key(APP_VERSION)


def signed_payload(manifest: dict) -> bytes:
    payload = {key: manifest.get(key) for key in SIGNED_FIELDS}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _public_key():
    path = _resource_path("assets", "update_public_key.pem")
    if not path.exists():
        raise UpdateError("安装包缺少更新公钥，不能安全检查更新")
    return serialization.load_pem_public_key(path.read_bytes())


def verify_manifest(manifest: dict) -> dict:
    if manifest.get("schema_version") != UPDATE_MANIFEST_SCHEMA:
        raise UpdateError("更新清单版本不受支持")
    for key in (*SIGNED_FIELDS, "signature"):
        if manifest.get(key) in (None, ""):
            raise UpdateError(f"更新清单缺少字段：{key}")
    if manifest.get("arch") not in {"arm64", "universal2"}:
        raise UpdateError("更新包不支持当前 Mac 架构")
    if manifest.get("bundle_id") != "app.caddie.alpha":
        raise UpdateError("更新包的应用标识不正确")
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", "")).lower()):
        raise UpdateError("更新包摘要格式不正确")
    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
        _public_key().verify(signature, signed_payload(manifest))
    except (ValueError, InvalidSignature) as exc:
        raise UpdateError("更新清单签名无效，已停止更新") from exc
    return manifest


def _allow_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if os.environ.get("CADDIE_UPDATE_ALLOW_LOCAL") == "1" and parsed.scheme in {"file", "http"}:
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise UpdateError("本地测试只允许访问 localhost")
        return
    raise UpdateError("更新地址必须使用 HTTPS")


def _fetch_bytes(url: str, limit: int) -> bytes:
    _allow_url(url)
    parsed = urlparse(url)
    if parsed.scheme == "file":
        data = Path(parsed.path).read_bytes()
        if len(data) > limit:
            raise UpdateError("更新文件超过允许大小")
        return data
    response = requests.get(url, timeout=(8, 30), stream=True)
    response.raise_for_status()
    chunks, size = [], 0
    for chunk in response.iter_content(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise UpdateError("更新文件超过允许大小")
        chunks.append(chunk)
    return b"".join(chunks)


def check(manifest_url: str | None = None) -> dict:
    url = (manifest_url or current_state()["manifest_url"]).strip()
    if not url:
        return {
            "status": "not_configured",
            "current_version": APP_VERSION,
            "message": "尚未配置更新通道",
        }
    try:
        raw = _fetch_bytes(url, MAX_MANIFEST_BYTES)
        manifest = verify_manifest(json.loads(raw.decode("utf-8")))
        available = is_newer(manifest["version"])
        _state(
            status="available" if available else "current",
            manifest=manifest,
            manifest_url=url,
            error=None,
            last_checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        return {
            "status": "available" if available else "current",
            "available": available,
            "current_version": APP_VERSION,
            "manifest": manifest,
        }
    except (OSError, ValueError, requests.RequestException, UpdateError) as exc:
        _state(status="check_failed", error=str(exc))
        raise UpdateError(f"检查更新失败：{exc}") from exc


def _manifest_from_state() -> dict:
    manifest = _read_json(STATE_PATH, {}).get("manifest")
    if not isinstance(manifest, dict):
        raise UpdateError("请先检查更新")
    return verify_manifest(manifest)


def download() -> dict:
    manifest = _manifest_from_state()
    if not is_newer(manifest["version"]):
        raise UpdateError("当前已经是最新版本")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATE_DIR / f"Caddie-{manifest['version']}.zip"
    part = target.with_suffix(".zip.part")
    part.unlink(missing_ok=True)
    _allow_url(manifest["download_url"])
    parsed = urlparse(manifest["download_url"])
    digest, size = hashlib.sha256(), 0
    try:
        if parsed.scheme == "file":
            source = open(parsed.path, "rb")
            response = None
        else:
            response = requests.get(manifest["download_url"], timeout=(8, 120), stream=True)
            response.raise_for_status()
            source = response.raw
        with source, open(part, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_PACKAGE_BYTES:
                    raise UpdateError("更新包超过 500 MB，已停止下载")
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        part.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != manifest["sha256"].lower():
        part.unlink(missing_ok=True)
        raise UpdateError("更新包校验失败，文件可能损坏或被篡改")
    part.replace(target)
    _state(status="downloaded", package_path=str(target), downloaded_bytes=size, error=None)
    return {"status": "downloaded", "package_path": str(target), "bytes": size, "manifest": manifest}


def _safe_extract(package: Path, target: Path):
    with zipfile.ZipFile(package) as archive:
        root = target.resolve()
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if root != destination and root not in destination.parents:
                raise UpdateError("更新包包含不安全路径")
    result = subprocess.run(
        ["/usr/bin/ditto", "-x", "-k", str(package), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise UpdateError(f"更新包解压失败：{result.stderr.strip()[:200]}")


def _find_staged_app(root: Path) -> Path:
    candidates = list(root.rglob("Caddie.app"))
    if len(candidates) != 1:
        raise UpdateError("更新包中没有唯一的 Caddie.app")
    return candidates[0]


def _validate_app(app_path: Path, expected_version: str):
    info_path = app_path / "Contents" / "Info.plist"
    executable = app_path / "Contents" / "MacOS" / "Caddie"
    if not info_path.exists() or not executable.exists():
        raise UpdateError("更新包中的应用结构不完整")
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != "app.caddie.alpha":
        raise UpdateError("更新包应用标识不匹配")
    if info.get("CFBundleShortVersionString") != expected_version:
        raise UpdateError("更新包版本与清单不匹配")
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise UpdateError("更新包内部签名结构无效")


def _backup_database(version: str) -> Path | None:
    source = DATA_DIR / "caddie.db"
    if not source.exists():
        return None
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"caddie-before-{version}-{stamp}.db"
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def prepare() -> dict:
    if not getattr(sys, "frozen", False):
        raise UpdateError("应用替换只能在打包版 Caddie 中执行")
    manifest = _manifest_from_state()
    package = Path(_read_json(STATE_PATH, {}).get("package_path") or "")
    if not package.exists():
        raise UpdateError("请先下载更新包")
    if hashlib.sha256(package.read_bytes()).hexdigest() != manifest["sha256"].lower():
        raise UpdateError("更新包二次校验失败")
    stage_root = UPDATE_DIR / f"staged-{manifest['version']}"
    shutil.rmtree(stage_root, ignore_errors=True)
    stage_root.mkdir(parents=True)
    _safe_extract(package, stage_root)
    staged_app = _find_staged_app(stage_root)
    _validate_app(staged_app, manifest["version"])
    backup_db = _backup_database(manifest["version"])
    _state(
        status="prepared",
        staged_app=str(staged_app),
        database_backup=str(backup_db) if backup_db else None,
        error=None,
    )
    return {
        "status": "prepared",
        "staged_app": str(staged_app),
        "database_backup": str(backup_db) if backup_db else None,
        "manifest": manifest,
    }


def current_app_path() -> Path:
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent
    raise UpdateError("无法定位当前 Caddie.app")


def create_apply_helper() -> dict:
    state = _read_json(STATE_PATH, {})
    staged_app = Path(state.get("staged_app") or "")
    if state.get("status") != "prepared" or not staged_app.exists():
        raise UpdateError("更新尚未准备完成")
    target_app = current_app_path()
    helper = UPDATE_DIR / "apply-update.sh"
    log_path = UPDATE_DIR / "apply-update.log"
    backup_app = UPDATE_DIR / "previous-Caddie.app"

    def q(value: Path | str) -> str:
        import shlex
        return shlex.quote(str(value))

    script = f"""#!/bin/zsh
set -eu
PID={os.getpid()}
TARGET={q(target_app)}
STAGED={q(staged_app)}
BACKUP={q(backup_app)}
LOG={q(log_path)}
exec >>"$LOG" 2>&1
echo "update started $(date)"
for i in {{1..120}}; do
  if ! /bin/kill -0 "$PID" 2>/dev/null; then break; fi
  /bin/sleep 0.25
done
if /bin/kill -0 "$PID" 2>/dev/null; then
  echo "old process did not exit"
  exit 1
fi
/bin/rm -rf "$BACKUP"
/bin/mv "$TARGET" "$BACKUP"
if ! /usr/bin/ditto "$STAGED" "$TARGET"; then
  /bin/rm -rf "$TARGET"
  /bin/mv "$BACKUP" "$TARGET"
  exit 1
fi
/usr/bin/xattr -dr com.apple.quarantine "$TARGET" || true
if ! /usr/bin/codesign --verify --deep --strict "$TARGET"; then
  /bin/rm -rf "$TARGET"
  /bin/mv "$BACKUP" "$TARGET"
  exit 1
fi
if ! /usr/bin/open "$TARGET"; then
  /bin/rm -rf "$TARGET"
  /bin/mv "$BACKUP" "$TARGET"
  /usr/bin/open "$TARGET" || true
  exit 1
fi
echo "update applied $(date)"
"""
    helper.write_text(script, encoding="utf-8")
    helper.chmod(0o700)
    return {
        "helper_path": str(helper),
        "target_app": str(target_app),
        "backup_app": str(backup_app),
        "log_path": str(log_path),
    }


def launch_apply_helper() -> dict:
    info = create_apply_helper()
    subprocess.Popen(
        ["/bin/zsh", info["helper_path"]],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _state(status="applying", **info)
    return {"status": "applying", **info}
