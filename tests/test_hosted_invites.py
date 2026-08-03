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

    @staticmethod
    def activation(code, device_id="mac-test"):
        return {
            "invite_code": code,
            "device_id": device_id,
            "device_name": "Test Mac",
            "email": f"{device_id}@example.com",
            "display_name": "测试用户",
        }

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
            json=self.activation(code),
        )
        self.assertEqual(activated.status_code, 200)
        payload = activated.json()
        self.assertTrue(payload["credential"].startswith("caddie_"))
        self.assertEqual(payload["total_tokens_limit"], 1_000_000)
        self.assertEqual(payload["daily_calls_limit"], 20)
        reused = self.client.post(
            "/v1/activate",
            json=self.activation(code, "mac-other"),
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
            json=self.activation(batch.json()["invite_codes"][0], "mac-linked"),
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
            "/v1/activate", json=self.activation(code)
        ).json()["credential"]
        usage = self.client.get(
            "/v1/usage", headers={"Authorization": f"Bearer {credential}"}
        )
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(usage.json()["daily_tokens_limit"], 150_000)
        self.assertEqual(usage.json()["daily_calls_limit"], 20)
        self.assertEqual(usage.json()["total_tokens_limit"], 1_000_000)
        self.assertIn("total_usage", usage.json())
        self.assertEqual(usage.json()["email"], "mac-test@example.com")
        self.assertTrue(usage.json()["profile_completed"])

    def test_admin_can_pause_and_extend_account(self):
        code = self.client.post(
            "/admin/v1/invites/batch", headers=self.admin, json={"count": 1}
        ).json()["invite_codes"][0]
        account_id = self.client.post(
            "/v1/activate", json=self.activation(code)
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

    @patch("gateway.app.requests.post")
    def test_telemetry_is_linked_without_personal_fields(self, post):
        gateway.SUPABASE_URL = "https://example.supabase.co"
        gateway.SUPABASE_SERVICE_ROLE_KEY = "service-secret"
        post.return_value.status_code = 201
        code = self.client.post(
            "/admin/v1/invites/batch", headers=self.admin,
            json={"count": 1, "batch_name": "cohort-a"},
        ).json()["invite_codes"][0]
        activated = self.client.post("/v1/activate", json=self.activation(code)).json()
        response = self.client.post(
            "/v1/telemetry/events",
            headers={"Authorization": f"Bearer {activated['credential']}"},
            json={"events": [{
                "event_id": "11111111-1111-4111-8111-111111111111",
                "event_name": "view_opened", "schema_version": 1,
                "installation_id": "22222222-2222-4222-8222-222222222222",
                "properties": {"view_name": "home"},
                "client_time": "2026-08-04T00:00:00+00:00",
                "app_version": "0.1.7-alpha", "platform": "macos",
                "email": "must-not-pass@example.com",
            }]},
        )
        self.assertEqual(response.status_code, 200)
        sent = post.call_args.kwargs["json"][0]
        self.assertEqual(sent["account_id"], activated["account_id"])
        self.assertEqual(sent["cohort_id"], "cohort-a")
        self.assertNotIn("email", sent)

    @patch("gateway.app.requests.get")
    def test_admin_dashboard_aggregates_events_by_named_user(self, get):
        gateway.SUPABASE_URL = "https://example.supabase.co"
        gateway.SUPABASE_SERVICE_ROLE_KEY = "service-secret"
        get.return_value.json.return_value = [{
            "event_name": "feature_action_completed",
            "account_id": None,
            "properties": {"feature": "sources"},
            "client_time": "2026-08-04T10:00:00+00:00",
            "app_version": "0.1.7-alpha",
        }]
        get.return_value.raise_for_status.return_value = None
        code = self.client.post(
            "/admin/v1/invites/batch", headers=self.admin,
            json={"count": 1, "batch_name": "cohort-a"},
        ).json()["invite_codes"][0]
        activated = self.client.post("/v1/activate", json=self.activation(code)).json()
        get.return_value.json.return_value[0]["account_id"] = activated["account_id"]
        result = self.client.get(
            "/admin/v1/analytics/overview?days=7", headers=self.admin,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["active_users"], 1)
        self.assertEqual(result.json()["registered_devices"], 1)
        self.assertEqual(result.json()["active_devices"], 1)
        self.assertEqual(result.json()["features"][0]["installations"], 0)
        self.assertEqual(result.json()["users"][0]["email"], "mac-test@example.com")
        self.assertEqual(result.json()["users"][0]["features"]["sources"], 1)


if __name__ == "__main__":
    unittest.main()
