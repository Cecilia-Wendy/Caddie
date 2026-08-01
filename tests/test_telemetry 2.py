import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import telemetry


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "caddie.db"
        db.init_db()
        telemetry.ensure_required_analytics()
        telemetry.clear_events()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_required_telemetry_cannot_be_disabled(self):
        telemetry.set_consent(False)
        self.assertTrue(telemetry.get_settings()["enabled"])
        telemetry.clear_events()
        self.assertTrue(telemetry.log_event("view_opened", {"view_name": "home"}))

    def test_registered_event_is_local_and_sanitized(self):
        event_id = telemetry.log_event(
            "source_added",
            {
                "source_type": "resume",
                "origin": "upload",
                "size_bucket": "1k_10k",
                "content": "王玺 18000000000 secret",
                "path": "/Users/example/private/resume.docx",
                "unknown": "should-not-pass",
            },
            entity_type="source",
            entity_id=12,
        )
        self.assertTrue(event_id)
        event = telemetry.list_events(1)[0]
        self.assertEqual(event["status"], "local")
        self.assertEqual(
            event["properties"],
            {"source_type": "resume", "origin": "upload", "size_bucket": "1k_10k"},
        )
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("王玺", serialized)
        self.assertNotIn("18000000000", serialized)
        self.assertNotIn("/Users/example", serialized)

    def test_unknown_event_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unregistered"):
            telemetry.log_event("button_clicked_everywhere", {"button": "x"})

    def test_product_and_ai_metrics_are_bucketed_without_content(self):
        telemetry.log_event(
            "feature_action_completed",
            {
                "feature": "sources",
                "action": "analyze",
                "status": "success",
                "duration_bucket": telemetry.duration_bucket(2600),
                "http_status": 200,
                "content": "private resume body",
            },
            session_id="session-test-1",
        )
        telemetry.log_event(
            "ai_call_completed",
            {
                "profile": "deep_reasoning",
                "status": "failed",
                "latency_bucket": telemetry.duration_bucket(35001),
                "input_tokens_bucket": telemetry.token_bucket(5200),
                "output_tokens_bucket": telemetry.token_bucket(0),
                "fallback_used": True,
                "retry_count": 1,
                "error_type": telemetry.classify_ai_error("request timeout"),
                "provider_type": "openai",
                "model_family": "glm",
                "prompt": "private prompt",
            },
        )
        events = telemetry.list_events(2)
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("private resume", serialized)
        self.assertNotIn("private prompt", serialized)
        self.assertEqual(events[0]["properties"]["error_type"], "timeout")
        self.assertEqual(events[1]["properties"]["duration_bucket"], "2s_10s")

    def test_untrusted_session_id_is_discarded(self):
        telemetry.log_event(
            "view_opened",
            {"view_name": "home"},
            session_id="private email@example.com",
        )
        conn = db.get_db()
        row = conn.execute(
            "SELECT session_id FROM telemetry_queue ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNone(row["session_id"])

    def test_external_agent_event_keeps_only_coarse_metadata(self):
        telemetry.log_event(
            "external_agent_tool_completed",
            {
                "client_type": "mcp",
                "agent_family": "codex",
                "tool_name": "read_context_package",
                "operation_class": "read",
                "status": "success",
                "duration_bucket": telemetry.duration_bucket(42),
                "result_count_bucket": telemetry.count_bucket(3),
                "error_type": "none",
                "content": "private context",
            },
        )
        event = telemetry.list_events(1)[0]
        self.assertEqual(event["properties"]["result_count_bucket"], "2_5")
        self.assertNotIn("content", event["properties"])

    @mock.patch("telemetry.requests.post")
    def test_upload_marks_events_as_uploaded(self, post):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        post.return_value = response
        telemetry.log_event("view_opened", {"view_name": "home"})
        with mock.patch.object(
            telemetry,
            "_upload_config",
            return_value={
                "project_url": "https://example.supabase.co",
                "publishable_key": "sb_publishable_test",
            },
        ):
            result = telemetry.upload_pending()
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(telemetry.list_events(1)[0]["status"], "uploaded")
        payload = post.call_args.kwargs["json"][0]
        self.assertEqual(payload["event_name"], "view_opened")
        self.assertNotIn("content", payload["properties"])


if __name__ == "__main__":
    unittest.main()
