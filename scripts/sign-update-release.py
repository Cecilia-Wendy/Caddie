#!/usr/bin/env python3
"""Create a signed update manifest for an already-built Caddie release."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION, UPDATE_MANIFEST_SCHEMA
from updater import signed_payload


PRIVATE_KEY = ROOT / ".release-private" / "update-signing-key.pem"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-url", required=True, help="Final HTTPS URL of the release ZIP")
    parser.add_argument("--notes", default="稳定性改进与体验优化")
    parser.add_argument("--allow-local", action="store_true", help="Allow file:// URL for local upgrade tests")
    parser.add_argument("--package", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "update-manifest-hosted-alpha.json",
    )
    args = parser.parse_args()

    package = args.package or ROOT / "dist" / f"Caddie-Hosted-Alpha-{APP_VERSION}-macos-arm64.zip"
    if not package.exists():
        raise SystemExit(f"Release package does not exist: {package}")
    if not args.download_url.startswith("https://") and not (
        args.allow_local and args.download_url.startswith("file://")
    ):
        raise SystemExit("Published download URL must use HTTPS")
    if not PRIVATE_KEY.exists():
        raise SystemExit("Missing private signing key. Run scripts/generate-update-keys.py first.")

    manifest = {
        "schema_version": UPDATE_MANIFEST_SCHEMA,
        "version": APP_VERSION,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "download_url": args.download_url,
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "arch": "arm64",
        "bundle_id": "app.caddie.hosted-alpha",
        "release_notes": args.notes,
        "package_bytes": package.stat().st_size,
    }
    private_key = serialization.load_pem_private_key(PRIVATE_KEY.read_bytes(), password=None)
    manifest["signature"] = base64.b64encode(private_key.sign(signed_payload(manifest))).decode("ascii")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Signed {package.name}")
    print(f"Manifest: {args.output}")
    print(f"SHA-256: {manifest['sha256']}")


if __name__ == "__main__":
    main()
