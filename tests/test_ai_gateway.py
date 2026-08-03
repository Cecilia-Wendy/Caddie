import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from gateway import app as gateway


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }


class AiGatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        gateway.DATA_DIR = Path(self.temp.name)
        gateway.DB_PATH = gateway.DATA_DIR / "gateway.sqlite3"
        self.env = patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "server-side-only",
            "CADDIE_GATEWAY_TESTERS_JSON": json.dumps({
                "tester-a": {"token": "token-a-long-secret", "daily_calls": 2, "daily_tokens": 100},
            }),
        })
        self.env.start()
        self.client = TestClient(gateway.app)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_rejects_missing_token(self):
        response = self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 401)

    @patch("gateway.app.requests.post", return_value=FakeResponse())
    def test_proxies_without_persisting_content_and_tracks_usage(self, upstream):
        headers = {"Authorization": "Bearer token-a-long-secret"}
        response = self.client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "ignored", "messages": [{"role": "user", "content": "private resume text"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "ok")
        sent = upstream.call_args.kwargs["json"]
        self.assertEqual(sent["model"], gateway.DEFAULT_MODEL)
        usage = self.client.get("/v1/usage", headers=headers).json()
        self.assertEqual((usage["calls"], usage["input_tokens"], usage["output_tokens"]), (1, 12, 3))
        raw_db = gateway.DB_PATH.read_bytes()
        self.assertNotIn(b"private resume text", raw_db)
        self.assertNotIn(b"server-side-only", raw_db)

    @patch("gateway.app.requests.post", return_value=FakeResponse())
    def test_enforces_per_tester_daily_call_limit(self, _upstream):
        headers = {"Authorization": "Bearer token-a-long-secret"}
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self.client.post("/v1/chat/completions", headers=headers, json=payload).status_code, 200)
        self.assertEqual(self.client.post("/v1/chat/completions", headers=headers, json=payload).status_code, 200)
        self.assertEqual(self.client.post("/v1/chat/completions", headers=headers, json=payload).status_code, 429)

    def test_deploy_script_exposes_gateway_only(self):
        script = (Path(__file__).parents[1] / "gateway" / "deploy-tencent.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("handle_path /gateway/*", script)
        self.assertIn("respond 404", script)
        self.assertNotIn("reverse_proxy caddie:8766", script)
        self.assertIn("tester-01", script)
        self.assertIn("tester-02", script)

if __name__ == "__main__":
    unittest.main()
