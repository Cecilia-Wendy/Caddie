import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from gateway import app as gateway


class HostedInviteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        gateway.DATA_DIR = Path(self.temp.name)
        gateway.DB_PATH = gateway.DATA_DIR / "gateway.sqlite3"
        gateway.ADMIN_TOKEN = "admin-test-secret"
        gateway.WEBSITE_CALLBACK_URL = ""
        gateway.GATEWAY_CALLBACK_TOKEN = ""
        self.client = TestClient(gateway.app)
        self.admin = {"Authorization": "Bearer admin-test-secret"}

    def tearDown(self):
        self.temp.cleanup()

    def test_invite_activation_issues_one_time_device_credential(self):
        batch = self.client.post(
            "/admin/v1/invites/batch",
            headers=self.admin,
            json={"count": 1, "batch_name": "day-1"},
        )
        self.assertEqual(batch.status_code, 200)
        code = batch.json()["invite_codes"][0]
        activated = self.client.post(
            "/v1/activate",
            json={"invite_code": code, "device_id": "mac-test", "device_name": "Test Mac"},
        )
        self.assertEqual(activated.status_code, 200)
        payload = activated.json()
        self.assertTrue(payload["credential"].startswith("caddie_"))
        self.assertEqual(payload["total_tokens_limit"], 2_000_000)
        self.assertEqual(payload["daily_calls_limit"], 30)
        reused = self.client.post(
            "/v1/activate",
            json={"invite_code": code, "device_id": "mac-other"},
        )
        self.assertEqual(reused.status_code, 409)
        raw_db = gateway.DB_PATH.read_bytes()
        self.assertNotIn(code.encode(), raw_db)
        self.assertNotIn(payload["credential"].encode(), raw_db)

    @patch("gateway.app.requests.post")
    def test_website_application_is_linked_and_activation_is_reported(self, post):
        gateway.WEBSITE_CALLBACK_URL = "https://caddie.example/api/internal/activations"
        gateway.GATEWAY_CALLBACK_TOKEN = "callback-secret"
        post.return_value.raise_for_status.return_value = None
        batch = self.client.post(
            "/admin/v1/invites/batch",
            headers=self.admin,
            json={"count": 1, "application_public_id": "AB12CD34"},
        )
        self.assertEqual(batch.status_code, 200)
        activated = self.client.post(
            "/v1/activate",
            json={
                "invite_code": batch.json()["invite_codes"][0],
                "device_id": "mac-linked",
            },
        )
        self.assertEqual(activated.status_code, 200)
        post.assert_called_once()
        callback = post.call_args.kwargs
        self.assertEqual(callback["json"]["application_public_id"], "AB12CD34")
        self.assertEqual(callback["headers"]["Authorization"], "Bearer callback-secret")
        linked = self.client.get(
            "/admin/v1/invites/by-application/AB12CD34", headers=self.admin
        )
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(linked.json()["status"], "activated")
        self.assertEqual(linked.json()["account_id"], activated.json()["account_id"])

    def test_activated_account_reports_daily_and_total_quota(self):
        code = self.client.post(
            "/admin/v1/invites/batch", headers=self.admin, json={"count": 1}
        ).json()["invite_codes"][0]
        credential = self.client.post(
            "/v1/activate", json={"invite_code": code, "device_id": "mac-test"}
        ).json()["credential"]
        usage = self.client.get(
            "/v1/usage", headers={"Authorization": f"Bearer {credential}"}
        )
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(usage.json()["daily_tokens_limit"], 300_000)
        self.assertEqual(usage.json()["total_tokens_limit"], 2_000_000)
        self.assertIn("total_usage", usage.json())

    def test_admin_can_pause_and_extend_account(self):
        code = self.client.post(
            "/admin/v1/invites/batch", headers=self.admin, json={"count": 1}
        ).json()["invite_codes"][0]
        account_id = self.client.post(
            "/v1/activate", json={"invite_code": code, "device_id": "mac-test"}
        ).json()["account_id"]
        paused = self.client.post(
            f"/admin/v1/accounts/{account_id}/status",
            headers=self.admin,
            json={"status": "paused"},
        )
        self.assertEqual(paused.status_code, 200)
        changed = self.client.post(
            f"/admin/v1/accounts/{account_id}/quota",
            headers=self.admin,
            json={"extend_days": 7, "total_tokens_limit": 3_000_000},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["total_tokens_limit"], 3_000_000)


if __name__ == "__main__":
    unittest.main()
