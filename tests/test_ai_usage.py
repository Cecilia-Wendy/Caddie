"""Local AI usage aggregation and hosted quota contract tests."""
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ai


class FakeUsageResponse:
    status_code = 200

    def json(self):
        return {
            "calls": 7,
            "input_tokens": 1100,
            "output_tokens": 400,
            "daily_calls_limit": 50,
            "daily_tokens_limit": 300000,
        }


class FakeBalanceResponse:
    status_code = 200

    def json(self):
        return {
            "is_available": True,
            "balance_infos": [{
                "currency": "CNY", "total_balance": "88.50",
                "granted_balance": "8.50", "topped_up_balance": "80.00",
            }],
        }


class AiUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.original = (
            ai.CONFIG_DIR, ai.CONFIG_PATH, ai.RUNTIME_PATH, ai.CALL_LOG_PATH,
            ai.requests.get,
        )
        ai.CONFIG_DIR = root
        ai.CONFIG_PATH = root / "config.json"
        ai.RUNTIME_PATH = root / "runtime.json"
        ai.CALL_LOG_PATH = root / "calls.jsonl"
        ai.save_config({
            "active_provider_id": "deepseek",
            "providers": [
                {
                    "id": "deepseek", "name": "我的 DeepSeek", "type": "openai",
                    "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                    "api_key": "secret", "monthly_token_limit": 10000,
                },
                {
                    "id": "hosted", "name": "Caddie Gateway", "type": "openai",
                    "base_url": "https://example.test/gateway/v1", "model": "deepseek-chat",
                    "api_key": "tester-token", "monthly_token_limit": 0,
                },
            ],
        })

    def tearDown(self):
        (
            ai.CONFIG_DIR, ai.CONFIG_PATH, ai.RUNTIME_PATH, ai.CALL_LOG_PATH,
            ai.requests.get,
        ) = self.original
        self.tmp.cleanup()

    def test_usage_is_grouped_by_provider_and_period(self):
        now = datetime.now()
        entries = [
            {
                "timestamp": now.isoformat(timespec="seconds"), "provider_id": "deepseek",
                "provider_name": "我的 DeepSeek", "model": "deepseek-chat", "status": "success",
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
            },
            {
                "timestamp": now.isoformat(timespec="seconds"), "provider_id": "deepseek",
                "provider_name": "我的 DeepSeek", "model": "deepseek-chat", "status": "failed",
                "usage": None,
            },
            {
                "timestamp": (now - timedelta(days=40)).isoformat(timespec="seconds"),
                "provider_id": "deepseek", "provider_name": "我的 DeepSeek",
                "model": "deepseek-chat", "status": "success",
                "usage": {"input_tokens": 600, "output_tokens": 200},
            },
        ]
        ai.CALL_LOG_PATH.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n",
            encoding="utf-8",
        )
        ai.requests.get = lambda url, **kwargs: (
            FakeBalanceResponse() if url.endswith("/user/balance") else FakeUsageResponse()
        )

        result = ai.usage_summary()
        deepseek = next(item for item in result["providers"] if item["provider_id"] == "deepseek")
        hosted = next(item for item in result["providers"] if item["provider_id"] == "hosted")
        self.assertEqual(deepseek["today"]["calls"], 2)
        self.assertEqual(deepseek["today"]["total_tokens"], 1500)
        self.assertEqual(deepseek["month"]["failed_calls"], 1)
        self.assertEqual(deepseek["all_time"]["total_tokens"], 2300)
        self.assertEqual(deepseek["monthly_remaining_tokens"], 8500)
        self.assertAlmostEqual(deepseek["monthly_usage_ratio"], 0.15)
        self.assertEqual(result["totals"]["today"]["calls"], 2)
        self.assertEqual(len(result["daily_series"]), 14)
        self.assertEqual(result["daily_series"][-1]["total_tokens"], 1500)
        self.assertEqual(hosted["remote_quota"]["daily_calls_limit"], 50)
        self.assertEqual(hosted["remote_quota"]["daily_tokens_limit"], 300000)
        self.assertEqual(deepseek["remote_quota"]["type"], "balance")
        self.assertEqual(deepseek["remote_quota"]["balances"][0]["total_balance"], "88.50")

    def test_remote_quota_is_optional_and_keys_are_never_returned(self):
        result = ai.usage_summary(include_remote=False)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("tester-token", serialized)
        self.assertTrue(all(item["remote_quota"] is None for item in result["providers"]))


if __name__ == "__main__":
    unittest.main()
