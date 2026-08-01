"""AI provider routing, privacy guard, and fallback smoke tests."""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ai


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class InvalidJsonResponse:
    status_code = 200

    def __init__(self, text=""):
        self.text = text

    def json(self):
        raise json.JSONDecodeError("Expecting value", self.text, 0)


def provider(pid, name, *, capabilities, data_policy="domestic"):
    return {
        "id": pid, "name": name, "type": "openai",
        "base_url": f"https://{pid}.example/v1", "model": f"{pid}-model",
        "api_key": f"key-{pid}", "capabilities": capabilities,
        "data_policy": data_policy,
    }


def main():
    original = (ai.CONFIG_DIR, ai.CONFIG_PATH, ai.RUNTIME_PATH, ai.CALL_LOG_PATH, ai.requests.post)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ai.CONFIG_DIR = root
        ai.CONFIG_PATH = root / "config.json"
        ai.RUNTIME_PATH = root / "runtime.json"
        ai.CALL_LOG_PATH = root / "calls.jsonl"

        primary = provider("primary", "主模型", capabilities=["text", "reasoning", "writing"])
        backup = provider("backup", "备用模型", capabilities=["text", "reasoning", "writing"])
        ai.save_config({
            "providers": [primary, backup], "active_provider_id": "primary",
            "model_profiles": {"deep_reasoning": ["primary", "backup"]},
        })

        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if "primary.example" in url:
                return FakeResponse(503, {"error": "temporary"})
            return FakeResponse(200, {
                "choices": [{"message": {"content": "备用成功"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            })

        ai.requests.post = fake_post
        selected = ai.resolve_model_profile("deep_reasoning")
        assert len(selected["chain"]) == 2, selected
        assert ai.chat([{"role": "user", "content": "test"}], provider=selected["provider"]) == "备用成功"
        assert sum("primary.example" in url for url in calls) == 2, calls
        assert sum("backup.example" in url for url in calls) == 1, calls
        call_info = ai.get_last_call_info()
        assert call_info["provider_id"] == "backup" and call_info["fallback_position"] == 1

        calls.clear()
        ai.RUNTIME_PATH.write_text('{"providers":{}}', encoding="utf-8")
        ai.requests.post = lambda url, **kwargs: (
            calls.append(url)
            or (InvalidJsonResponse() if "primary.example" in url else FakeResponse(200, {
                "choices": [{"message": {"content": "非 JSON 降级成功"}}],
                "usage": {},
            }))
        )
        assert ai.chat(
            [{"role": "user", "content": "test"}],
            provider={**primary, "_fallback_providers": [backup]},
        ) == "非 JSON 降级成功"
        assert sum("primary.example" in url for url in calls) == 2, calls
        assert sum("backup.example" in url for url in calls) == 1, calls

        try:
            ai._decode_provider_json(primary, InvalidJsonResponse("<html>gateway</html>"))
            raise AssertionError("invalid provider JSON must be retryable")
        except ai.AIError as exc:
            assert exc.retryable and "非 JSON 响应" in str(exc)

        quota_error = ai._friendly_http_error(primary, FakeResponse(429, {
            "error": {"type": "insufficient_quota", "message": "check billing details"}
        }))
        assert "额度不足" in str(quota_error) and not quota_error.retryable
        auth_error = ai._friendly_http_error(primary, FakeResponse(401, {
            "error": {"message": "invalid api key"}
        }))
        assert "API Key" in str(auth_error) and "invalid api key" not in str(auth_error)

        calls.clear()
        ai.requests.post = lambda url, **kwargs: (
            calls.append(url)
            or FakeResponse(429, {"error": {"message": "rate limited"}})
        )
        try:
            ai.chat(
                [{"role": "user", "content": "test"}],
                provider={**primary, "_fallback_providers": []},
            )
            raise AssertionError("rate limits must surface as failures")
        except ai.AIError as exc:
            assert "请求过于频繁" in str(exc)
        assert len(calls) == 1, calls

        ai.requests.post = lambda *args, **kwargs: FakeResponse(200, {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"completion_tokens_details": {"reasoning_tokens": 16}},
        })
        try:
            ai._chat_openai(primary, [{"role": "user", "content": "ping"}], "", 16, 10)
            raise AssertionError("empty model content must not count as success")
        except ai.AIError as exc:
            assert "内部推理" in str(exc)

        runtime = json.loads(ai.RUNTIME_PATH.read_text(encoding="utf-8"))
        assert runtime["providers"]["primary"]["status"] == "degraded"
        assert runtime["providers"]["backup"]["status"] == "healthy"
        log_text = ai.CALL_LOG_PATH.read_text(encoding="utf-8")
        assert "备用成功" not in log_text and "test" not in log_text
        assert '"profile_key": "deep_reasoning"' in log_text

        try:
            ai.resolve_model_profile("private")
            raise AssertionError("private profile must not fall back to cloud")
        except ai.AIError as exc:
            assert "不会自动发送到云端" in str(exc)

        local = provider("local", "本地模型", capabilities=["text", "private"], data_policy="local")
        cfg = ai.load_config()
        cfg["providers"].append(local)
        ai.save_config(cfg)
        assert ai.resolve_model_profile("private")["provider_id"] == "local"
        assert len(ai.resolve_model_profile("private")["chain"]) == 1

        rescue = provider("rescue", "自动备用", capabilities=["text", "reasoning", "writing"])
        cfg = ai.load_config()
        cfg["providers"].append(rescue)
        cfg["model_profiles"]["writing"] = ["primary"]
        ai.save_config(cfg)
        writing = ai.resolve_model_profile("writing")
        assert writing["provider_id"] == "primary"
        assert [item["id"] for item in writing["chain"]] == ["primary", "backup", "rescue"]

        assert oct(ai.CONFIG_PATH.stat().st_mode & 0o777) == "0o600"

    ai.CONFIG_DIR, ai.CONFIG_PATH, ai.RUNTIME_PATH, ai.CALL_LOG_PATH, ai.requests.post = original
    print("AI_FOUNDATION_OK")


if __name__ == "__main__":
    main()
