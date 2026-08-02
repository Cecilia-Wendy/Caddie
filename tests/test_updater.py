import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import updater


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = Ed25519PrivateKey.generate()
        self.manifest = {
            "schema_version": 1,
            "version": "9.9.9-alpha",
            "published_at": "2026-07-26T00:00:00+00:00",
            "download_url": "https://example.invalid/Caddie.zip",
            "sha256": "0" * 64,
            "arch": "arm64",
            "bundle_id": "app.caddie.hosted-alpha",
            "release_notes": "test",
        }

    def tearDown(self):
        self.temp.cleanup()

    def _sign(self):
        self.manifest["signature"] = base64.b64encode(
            self.private.sign(updater.signed_payload(self.manifest))
        ).decode("ascii")

    def test_signed_manifest_rejects_tampering(self):
        self._sign()
        with patch.object(updater, "_public_key", return_value=self.private.public_key()):
            updater.verify_manifest(self.manifest)
            self.manifest["download_url"] = "https://evil.invalid/Caddie.zip"
            with self.assertRaises(updater.UpdateError):
                updater.verify_manifest(self.manifest)

    def test_local_signed_check_and_download(self):
        package = self.root / "Caddie.zip"
        package.write_bytes(b"signed update bytes")
        self.manifest["download_url"] = package.as_uri()
        self.manifest["sha256"] = hashlib.sha256(package.read_bytes()).hexdigest()
        self._sign()
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        update_dir = self.root / "updates"
        with (
            patch.object(updater, "_public_key", return_value=self.private.public_key()),
            patch.object(updater, "UPDATE_DIR", update_dir),
            patch.object(updater, "STATE_PATH", update_dir / "state.json"),
            patch.dict(os.environ, {"CADDIE_UPDATE_ALLOW_LOCAL": "1"}),
        ):
            result = updater.check(manifest_path.as_uri())
            self.assertTrue(result["available"])
            downloaded = updater.download()
            self.assertEqual(Path(downloaded["package_path"]).read_bytes(), package.read_bytes())

    def test_zip_path_traversal_is_rejected(self):
        package = self.root / "unsafe.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("../outside.txt", "no")
        with self.assertRaises(updater.UpdateError):
            updater._safe_extract(package, self.root / "extract")

    def test_database_backup_is_readable(self):
        data_dir = self.root / "data"
        data_dir.mkdir()
        conn = sqlite3.connect(data_dir / "caddie.db")
        try:
            conn.execute("CREATE TABLE facts(value TEXT)")
            conn.execute("INSERT INTO facts VALUES ('kept')")
            conn.commit()
        finally:
            conn.close()
        with patch.object(updater, "DATA_DIR", data_dir):
            backup = updater._backup_database("9.9.9-alpha")
        conn = sqlite3.connect(backup)
        try:
            self.assertEqual(conn.execute("SELECT value FROM facts").fetchone()[0], "kept")
        finally:
            conn.close()

    def test_hosted_release_identity_is_consistent(self):
        root = Path(__file__).parents[1]
        signer = (root / "scripts" / "sign-update-release.py").read_text(encoding="utf-8")
        channel = json.loads((root / "assets" / "update_channel.json").read_text(encoding="utf-8"))
        self.assertEqual(updater.APP_BUNDLE_NAME, "Caddie Hosted Alpha.app")
        self.assertEqual(updater.APP_EXECUTABLE_NAME, "Caddie Hosted Alpha")
        self.assertEqual(updater.APP_BUNDLE_ID, "app.caddie.hosted-alpha")
        self.assertIn("update-manifest-hosted-alpha.json", channel["manifest_url"])
        self.assertIn('"bundle_id": "app.caddie.hosted-alpha"', signer)

    def test_global_update_indicator_checks_in_background(self):
        root = Path(__file__).parents[1]
        html = (root / "static" / "index.html").read_text(encoding="utf-8")
        css = (root / "static" / "caddie-ui.css").read_text(encoding="utf-8")
        self.assertIn('id="topbarUpdate" hidden', html)
        self.assertIn("window.setTimeout(()=>checkGlobalUpdate(),1400)", html)
        self.assertIn("window.addEventListener('focus',()=>checkGlobalUpdate())", html)
        self.assertIn("installCaddieUpdate()", html)
        self.assertIn(".topbar-update-dot", css)


if __name__ == "__main__":
    unittest.main()
