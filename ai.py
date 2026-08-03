"""
Caddie - AI 供应商抽象层
支持在多个 AI 供应商之间切换：Anthropic Claude / OpenAI / DeepSeek / Kimi / 智谱 / 硅基流动 / 本地 Ollama 等。

配置存储在 ~/.caddie/config.json：
{
  "providers": [
    {"id": "...", "name": "Claude", "type": "anthropic",
     "base_url": "https://api.anthropic.com", "api_key": "...", "model": "claude-sonnet-4-6"}
  ],
  "active_provider_id": "..."
}
"""
import json
import os
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import hosted_access

CONFIG_DIR = Path(os.environ.get("CADDIE_DATA_DIR") or (Path.home() / ".caddie"))
CONFIG_PATH = CONFIG_DIR / "config.json"
RUNTIME_PATH = CONFIG_DIR / "ai_runtime.json"
CALL_LOG_PATH = CONFIG_DIR / "ai_calls.jsonl"
PUBLIC_DEMO_QUOTA_PATH = CONFIG_DIR / "public_demo_ai_quota.json"

PROVIDER_RETRY_DELAY_SECONDS = 0.35
DEFAULT_CAPABILITIES = ["text", "structured_output", "long_context"]
_LAST_CALL_INFO: ContextVar[dict | None] = ContextVar("caddie_last_ai_call", default=None)
_PUBLIC_DEMO_QUOTA_LOCK = threading.Lock()

# ─── 预设供应商（GUI 里一键填充 base_url 和常用模型）──────────────────────────
# type: "anthropic" 走 /v1/messages，"openai" 走 /chat/completions（OpenAI 兼容）
PRESETS = [
    {
        "key": "caddie_hosted", "name": "Caddie 托管 AI", "type": "openai",
        "base_url": "https://43-128-7-135.sslip.io/gateway/v1",
        "models": ["deepseek-chat"],
        "key_hint": "在 Caddie 中输入邀请码激活，无需填写 API Key",
        "capabilities": ["text", "reasoning", "writing", "structured_output", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "anthropic", "name": "Claude (Anthropic)", "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "key_hint": "console.anthropic.com 获取，sk-ant- 开头",
        "capabilities": ["text", "reasoning", "writing", "vision", "tools", "long_context"],
        "data_policy": "overseas",
    },
    {
        "key": "openai", "name": "OpenAI", "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "key_hint": "platform.openai.com 获取，sk- 开头",
        "capabilities": ["text", "reasoning", "writing", "vision", "tools", "structured_output", "long_context"],
        "data_policy": "overseas",
    },
    {
        "key": "deepseek", "name": "DeepSeek", "type": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
        "key_hint": "platform.deepseek.com 获取",
        "capabilities": ["text", "reasoning", "writing", "structured_output", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "qwen", "name": "Qwen (阿里云百炼)", "type": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo"],
        "key_hint": "阿里云百炼控制台获取",
        "capabilities": ["text", "reasoning", "writing", "vision", "tools", "structured_output", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "doubao", "name": "豆包 (火山方舟)", "type": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-1-6-251015"],
        "key_hint": "火山方舟控制台获取；也可填写自己的推理接入点 ID",
        "capabilities": ["text", "reasoning", "writing", "vision", "tools", "structured_output", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "moonshot", "name": "Kimi (Moonshot)", "type": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "key_hint": "platform.moonshot.cn 获取",
        "capabilities": ["text", "reasoning", "writing", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "zhipu", "name": "智谱 GLM", "type": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.2", "glm-4.7-flashx", "glm-4.7-flash", "glm-5v-turbo", "glm-4.6v-flash"],
        "key_hint": "bigmodel.cn 获取；图片请单独添加一个 4.6V/4V 供应商并绑定到「图像理解」",
        "capabilities": ["text", "reasoning", "writing", "vision", "tools", "structured_output", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "siliconflow", "name": "硅基流动 SiliconFlow", "type": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "key_hint": "siliconflow.cn 获取（聚合多模型）",
        "capabilities": ["text", "reasoning", "writing", "vision", "structured_output", "long_context"],
        "data_policy": "domestic",
    },
    {
        "key": "ollama", "name": "本地 Ollama", "type": "openai",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5", "llama3.1", "deepseek-r1"],
        "key_hint": "本地运行，api_key 可留空填 ollama",
        "capabilities": ["text", "reasoning", "writing", "structured_output", "private"],
        "data_policy": "local",
    },
    {
        "key": "custom", "name": "自定义（OpenAI 兼容）", "type": "openai",
        "base_url": "",
        "models": [],
        "key_hint": "任何兼容 OpenAI /chat/completions 的服务",
        "capabilities": DEFAULT_CAPABILITIES,
        "data_policy": "unknown",
    },
]


MODEL_PROFILE_DEFS = {
    "deep_reasoning": {
        "name": "深度推理", "description": "复杂判断、经历审计、模拟追问与深度复盘",
        "recommended": "优先选择推理能力强、上下文较长的模型",
        "required_capability": "reasoning",
    },
    "writing": {
        "name": "内容写作", "description": "知识讲解、文档整理、简历与表达修改",
        "recommended": "优先选择中文表达稳定、长文结构清晰的模型",
        "required_capability": "writing",
    },
    "research": {
        "name": "研究分析", "description": "公司、岗位、行业和资料的综合研究",
        "recommended": "优先选择事实遵循好、长上下文能力强的模型",
        "required_capability": "long_context",
    },
    "fast": {
        "name": "快速处理", "description": "分类、抽取、打标和轻量整理",
        "recommended": "优先选择速度快、成本低的模型",
        "required_capability": "text",
    },
    "vision": {
        "name": "图像理解", "description": "简历截图、岗位图片、图表和视觉材料理解",
        "recommended": "选择支持图像输入的多模态模型",
        "required_capability": "vision",
    },
    "private": {
        "name": "本地隐私", "description": "敏感资料优先交给本地模型处理",
        "recommended": "仅使用本地模型；未配置时会停止，不会把敏感资料发送到云端",
        "required_capability": "private",
    },
}


# ─── 配置读写 ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"providers": [], "active_provider_id": None}


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def _read_runtime() -> dict:
    if RUNTIME_PATH.exists():
        try:
            return json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"providers": {}}


def _write_runtime(data: dict):
    CONFIG_DIR.mkdir(exist_ok=True)
    RUNTIME_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(RUNTIME_PATH, 0o600)


def _record_call(provider: dict, *, status: str, latency_ms: int, error: str = "",
                 usage: dict | None = None, retry_count: int = 0):
    runtime = _read_runtime()
    health = runtime.setdefault("providers", {}).setdefault(provider.get("id", "unknown"), {})
    health.update({
        "status": "healthy" if status == "success" else "degraded",
        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
        "last_latency_ms": latency_ms,
        "last_error": error[:240],
    })
    if status == "success":
        health["success_count"] = int(health.get("success_count", 0)) + 1
        health["consecutive_failures"] = 0
    else:
        health["failure_count"] = int(health.get("failure_count", 0)) + 1
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
    _write_runtime(runtime)

    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "provider_id": provider.get("id"), "provider_name": provider.get("name"),
        "model": provider.get("model"), "status": status, "latency_ms": latency_ms,
        "profile_key": provider.get("_profile_key"),
        "fallback_position": int(provider.get("_fallback_position") or 0),
        "error": error[:240] or None, "usage": usage or None,
    }
    with CALL_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.chmod(CALL_LOG_PATH, 0o600)
    try:
        import telemetry
        usage = usage or {}
        model_name = str(provider.get("model") or "").lower()
        if "claude" in model_name:
            model_family = "claude"
        elif "gpt" in model_name or model_name.startswith("o1") or model_name.startswith("o3"):
            model_family = "openai"
        elif "deepseek" in model_name:
            model_family = "deepseek"
        elif "glm" in model_name:
            model_family = "glm"
        elif "qwen" in model_name:
            model_family = "qwen"
        elif "doubao" in model_name:
            model_family = "doubao"
        elif "llama" in model_name:
            model_family = "llama"
        else:
            model_family = "other"
        telemetry.log_event("ai_call_completed", {
            "profile": provider.get("_profile_key") or "direct",
            "status": status,
            "latency_bucket": telemetry.duration_bucket(latency_ms),
            "input_tokens_bucket": telemetry.token_bucket(
                usage.get("prompt_tokens") or usage.get("input_tokens")
            ),
            "output_tokens_bucket": telemetry.token_bucket(
                usage.get("completion_tokens") or usage.get("output_tokens")
            ),
            "fallback_used": int(provider.get("_fallback_position") or 0) > 0,
            "retry_count": retry_count,
            "error_type": telemetry.classify_ai_error(error),
            "provider_type": provider.get("type") or "openai_compatible",
            "model_family": model_family,
        })
    except Exception:
        # Instrumentation must never turn a successful model response into a
        # product failure.
        pass


def _consume_public_demo_quota():
    """Reserve one public-demo AI call using a persistent daily counter."""
    raw_limit = os.environ.get("CADDIE_PUBLIC_AI_DAILY_LIMIT", "").strip()
    if not raw_limit:
        return
    try:
        limit = max(0, int(raw_limit))
    except ValueError:
        raise AIError("公开体验的 AI 调用额度配置无效。")
    today = datetime.now().date().isoformat()
    with _PUBLIC_DEMO_QUOTA_LOCK:
        state = {"date": today, "count": 0}
        if PUBLIC_DEMO_QUOTA_PATH.exists():
            try:
                loaded = json.loads(PUBLIC_DEMO_QUOTA_PATH.read_text(encoding="utf-8"))
                if loaded.get("date") == today:
                    state = loaded
            except Exception:
                pass
        count = int(state.get("count") or 0)
        if count >= limit:
            raise AIError(f"今日公开体验额度已用完（{limit} 次），请明天再试。", status_code=429)
        state["count"] = count + 1
        CONFIG_DIR.mkdir(exist_ok=True)
        PUBLIC_DEMO_QUOTA_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(PUBLIC_DEMO_QUOTA_PATH, 0o600)


def list_recent_calls(limit: int = 50) -> list[dict]:
    """Return local operational metadata only; prompts and responses are never logged."""
    if not CALL_LOG_PATH.exists():
        return []
    lines = CALL_LOG_PATH.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]
    result = []
    for line in reversed(lines):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return result


def _usage_tokens(usage: dict | None) -> tuple[int, int]:
    """Normalize OpenAI-compatible and Anthropic token fields."""
    usage = usage or {}
    input_tokens = usage.get("prompt_tokens")
    if input_tokens is None:
        input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("completion_tokens")
    if output_tokens is None:
        output_tokens = usage.get("output_tokens")
    try:
        input_tokens = max(0, int(input_tokens or 0))
    except (TypeError, ValueError):
        input_tokens = 0
    try:
        output_tokens = max(0, int(output_tokens or 0))
    except (TypeError, ValueError):
        output_tokens = 0
    return input_tokens, output_tokens


def _empty_usage_period() -> dict:
    return {
        "calls": 0,
        "successful_calls": 0,
        "failed_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def _remote_provider_usage(provider: dict) -> dict | None:
    """Read account data only from provider endpoints supported by a normal API key."""
    base_url = str(provider.get("base_url") or "").rstrip("/")
    api_key = _provider_api_key(provider)
    if not api_key:
        return None
    identity = " ".join(str(provider.get(key) or "") for key in ("name", "base_url", "model")).lower()
    if "/gateway/v1" in base_url.lower():
        url = f"{base_url}/usage"
        usage_type = "daily_quota"
    elif "api.deepseek.com" in identity:
        url = f"{base_url.removesuffix('/v1')}/user/balance"
        usage_type = "balance"
    else:
        # OpenAI/Anthropic organization usage requires a separate admin key;
        # Alibaba monitoring requires AccessKey credentials. Never send a
        # normal model key to non-standard billing endpoints speculatively.
        return None
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.RequestException, ValueError, TypeError):
        return None
    if usage_type == "balance":
        balances = []
        for item in payload.get("balance_infos") or []:
            if not isinstance(item, dict):
                continue
            balances.append({
                "currency": str(item.get("currency") or ""),
                "total_balance": str(item.get("total_balance") or "0"),
                "granted_balance": str(item.get("granted_balance") or "0"),
                "topped_up_balance": str(item.get("topped_up_balance") or "0"),
            })
        return {"type": usage_type, "is_available": bool(payload.get("is_available")), "balances": balances}
    allowed = {
        "calls", "input_tokens", "output_tokens", "daily_calls_limit",
        "daily_tokens_limit", "usage_date", "expires_at", "total_usage",
        "total_tokens_limit", "account_status",
    }
    return {
        "type": usage_type,
        **{key: payload.get(key) for key in allowed if payload.get(key) is not None},
    }


def usage_summary(*, include_remote: bool = True) -> dict:
    """Aggregate local AI usage without reading or exposing prompts and responses."""
    now = datetime.now()
    today = now.date()
    month_key = now.strftime("%Y-%m")
    series_days = [(today - timedelta(days=offset)) for offset in range(13, -1, -1)]
    daily_series = {
        day.isoformat(): {
            "date": day.isoformat(), "calls": 0, "input_tokens": 0,
            "output_tokens": 0, "total_tokens": 0,
        }
        for day in series_days
    }
    configured = load_config().get("providers", [])
    provider_rows = {}
    for provider in configured:
        raw_limit = provider.get("monthly_token_limit")
        try:
            monthly_limit = max(0, int(raw_limit or 0))
        except (TypeError, ValueError):
            monthly_limit = 0
        provider_rows[provider.get("id")] = {
            "provider_id": provider.get("id"),
            "provider_name": provider.get("name") or "未命名供应商",
            "model": provider.get("model") or "",
            "monthly_token_limit": monthly_limit,
            "today": _empty_usage_period(),
            "month": _empty_usage_period(),
            "all_time": _empty_usage_period(),
            "remote_quota": _remote_provider_usage(provider) if include_remote else None,
        }

    if CALL_LOG_PATH.exists():
        for line in CALL_LOG_PATH.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                timestamp = datetime.fromisoformat(str(entry.get("timestamp") or ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            provider_id = entry.get("provider_id") or "unknown"
            if provider_id not in provider_rows:
                provider_rows[provider_id] = {
                    "provider_id": provider_id,
                    "provider_name": entry.get("provider_name") or "已删除供应商",
                    "model": entry.get("model") or "",
                    "monthly_token_limit": 0,
                    "today": _empty_usage_period(),
                    "month": _empty_usage_period(),
                    "all_time": _empty_usage_period(),
                    "remote_quota": None,
                }
            input_tokens, output_tokens = _usage_tokens(entry.get("usage"))
            day_key = timestamp.date().isoformat()
            if day_key in daily_series:
                daily_series[day_key]["calls"] += 1
                daily_series[day_key]["input_tokens"] += input_tokens
                daily_series[day_key]["output_tokens"] += output_tokens
                daily_series[day_key]["total_tokens"] += input_tokens + output_tokens
            periods = [provider_rows[provider_id]["all_time"]]
            if timestamp.strftime("%Y-%m") == month_key:
                periods.append(provider_rows[provider_id]["month"])
            if timestamp.date() == today:
                periods.append(provider_rows[provider_id]["today"])
            for period in periods:
                period["calls"] += 1
                if entry.get("status") == "success":
                    period["successful_calls"] += 1
                else:
                    period["failed_calls"] += 1
                period["input_tokens"] += input_tokens
                period["output_tokens"] += output_tokens
                period["total_tokens"] += input_tokens + output_tokens

    providers = list(provider_rows.values())
    total = {"today": _empty_usage_period(), "month": _empty_usage_period(), "all_time": _empty_usage_period()}
    for provider in providers:
        for period_name in total:
            for key in total[period_name]:
                total[period_name][key] += provider[period_name][key]
        limit = provider["monthly_token_limit"]
        used = provider["month"]["total_tokens"]
        provider["monthly_remaining_tokens"] = max(0, limit - used) if limit else None
        provider["monthly_usage_ratio"] = min(1, used / limit) if limit else None
    providers.sort(key=lambda item: (item["month"]["total_tokens"], item["today"]["calls"]), reverse=True)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "today": date.today().isoformat(),
        "month": month_key,
        "totals": total,
        "providers": providers,
        "daily_series": list(daily_series.values()),
    }


def get_last_call_info() -> dict | None:
    info = _LAST_CALL_INFO.get()
    return dict(info) if info else None


def foundation_status() -> dict:
    profiles = list_model_profiles()
    operational_keys = {"deep_reasoning", "writing", "research", "fast"}
    operational = [item for item in profiles if item["key"] in operational_keys]
    unavailable = [item for item in operational if not item.get("available")]
    without_backup = [item for item in operational if item.get("available") and len(item.get("chain") or []) < 2]
    private = next((item for item in profiles if item["key"] == "private"), None)
    recent = list_recent_calls(30)
    fallback_successes = sum(
        1 for item in recent
        if item.get("status") == "success" and int(item.get("fallback_position") or 0) > 0
    )
    warnings = []
    if unavailable:
        warnings.append("这些核心能力尚无可用模型：" + "、".join(x["name"] for x in unavailable))
    if without_backup:
        warnings.append("这些能力只有一个模型，故障时无法自动切换：" + "、".join(x["name"] for x in without_backup))
    if not (private and private.get("available")):
        warnings.append("尚未配置本地隐私模型；隐私任务会停止，不会发送到云端")
    return {
        "status": "ready" if not unavailable else "needs_attention",
        "provider_count": len(load_config().get("providers", [])),
        "available_core_profiles": len(operational) - len(unavailable),
        "core_profile_count": len(operational),
        "protected_private": bool(private and private.get("available")),
        "fallback_successes": fallback_successes,
        "warnings": warnings,
        "profiles": [{
            "key": item["key"], "name": item["name"], "available": item["available"],
            "chain_length": len(item.get("chain") or []),
            "provider_name": item.get("provider_name"), "model": item.get("model"),
        } for item in profiles],
    }


def _normalise_capabilities(provider: dict) -> list[str]:
    capabilities = provider.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        return list(dict.fromkeys(str(x) for x in capabilities if x))
    identity = " ".join(str(provider.get(k) or "") for k in ("name", "base_url", "model")).lower()
    if provider.get("data_policy") == "local" or provider.get("type") == "ollama" or "localhost:11434" in identity:
        return ["text", "reasoning", "writing", "structured_output", "private"]
    if provider.get("type") == "anthropic" or "anthropic" in identity or "claude" in identity:
        return ["text", "reasoning", "writing", "vision", "tools", "long_context"]
    if any(token in identity for token in ("bigmodel", "glm", "qwen", "dashscope", "moonshot", "kimi", "siliconflow")):
        return ["text", "reasoning", "writing", "vision", "tools", "structured_output", "long_context"]
    if any(token in identity for token in ("deepseek", "volces", "doubao")):
        return ["text", "reasoning", "writing", "structured_output", "long_context"]
    return list(DEFAULT_CAPABILITIES)


def _normalise_data_policy(provider: dict) -> str:
    if provider.get("data_policy") in {"local", "domestic", "overseas", "unknown"}:
        return provider["data_policy"]
    identity = " ".join(str(provider.get(k) or "") for k in ("name", "base_url")).lower()
    if "localhost" in identity or "127.0.0.1" in identity:
        return "local"
    if any(token in identity for token in ("deepseek", "moonshot.cn", "bigmodel.cn", "siliconflow.cn", "aliyuncs.com", "volces.com")):
        return "domestic"
    if any(token in identity for token in ("openai.com", "anthropic.com", "moonshot.ai")):
        return "overseas"
    return "unknown"


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return key[:6] + "···" + key[-4:]


def _provider_api_key(provider: dict) -> str:
    value = str(provider.get("api_key") or "")
    if value == hosted_access.KEYCHAIN_MARKER:
        return hosted_access.credential()
    return value


def install_hosted_provider() -> dict:
    """Create or refresh the managed provider without persisting its credential."""
    cfg = load_config()
    existing = next(
        (item for item in cfg.get("providers", []) if item.get("id") == "caddie-hosted"),
        None,
    )
    result = upsert_provider({
        "id": "caddie-hosted",
        "name": "Caddie 托管 AI",
        "type": "openai",
        "base_url": hosted_access.DEFAULT_BASE_URL,
        "model": "deepseek-chat",
        "api_key": hosted_access.KEYCHAIN_MARKER,
        "capabilities": ["text", "reasoning", "writing", "structured_output", "long_context"],
        "data_policy": "domestic",
    })
    set_active(result["id"])
    if not existing:
        for profile in MODEL_PROFILE_DEFS:
            set_model_profile(profile, [result["id"]])
    return result


def list_providers_masked() -> dict:
    """给前端用：api_key 打码。"""
    cfg = load_config()
    providers = []
    for p in cfg.get("providers", []):
        q = dict(p)
        q["capabilities"] = _normalise_capabilities(q)
        q["data_policy"] = _normalise_data_policy(q)
        q["api_key_masked"] = _mask(p.get("api_key", ""))
        q["has_key"] = bool(p.get("api_key"))
        q.pop("api_key", None)
        providers.append(q)
    runtime = _read_runtime().get("providers", {})
    for q in providers:
        q["health"] = runtime.get(q.get("id"), {"status": "unknown"})
    return {
        "providers": providers,
        "active_provider_id": cfg.get("active_provider_id"),
        "presets": PRESETS,
        "model_profiles": list_model_profiles(),
    }


def upsert_provider(data: dict) -> dict:
    """新增或更新一个供应商。若 api_key 为空字符串则保留原 key（前端打码不回传）。"""
    cfg = load_config()
    providers = cfg.get("providers", [])
    pid = data.get("id")

    incoming_key = data.get("api_key", "")
    record = {
        "id": pid or uuid.uuid4().hex[:12],
        "name": data.get("name", "未命名"),
        "type": data.get("type", "openai"),
        "base_url": (data.get("base_url") or "").rstrip("/"),
        "model": data.get("model", ""),
        "api_key": incoming_key,
        "capabilities": list(dict.fromkeys(data.get("capabilities") or DEFAULT_CAPABILITIES)),
        "data_policy": data.get("data_policy") or ("local" if data.get("type") == "ollama" else "unknown"),
        "monthly_token_limit": max(0, int(data.get("monthly_token_limit") or 0)),
    }

    found = False
    for i, p in enumerate(providers):
        if p["id"] == record["id"]:
            if not incoming_key:          # 没传新 key → 保留旧的
                record["api_key"] = p.get("api_key", "")
            if not data.get("capabilities"):
                record["capabilities"] = _normalise_capabilities(p)
            if data.get("data_policy") in (None, ""):
                record["data_policy"] = _normalise_data_policy(p)
            if data.get("monthly_token_limit") is None:
                record["monthly_token_limit"] = max(0, int(p.get("monthly_token_limit") or 0))
            providers[i] = record
            found = True
            break
    if not found:
        providers.append(record)

    cfg["providers"] = providers
    if not cfg.get("active_provider_id"):
        cfg["active_provider_id"] = record["id"]
    save_config(cfg)
    return {"id": record["id"]}


def delete_provider(pid: str):
    cfg = load_config()
    cfg["providers"] = [p for p in cfg.get("providers", []) if p["id"] != pid]
    if cfg.get("active_provider_id") == pid:
        cfg["active_provider_id"] = cfg["providers"][0]["id"] if cfg["providers"] else None
    save_config(cfg)


def set_active(pid: str):
    cfg = load_config()
    cfg["active_provider_id"] = pid
    save_config(cfg)


def get_active_provider() -> dict | None:
    cfg = load_config()
    pid = cfg.get("active_provider_id")
    for p in cfg.get("providers", []):
        if p["id"] == pid:
            return p
    return None


def _profile_chain_ids(cfg: dict, profile_key: str) -> list[str]:
    value = (cfg.get("model_profiles") or {}).get(profile_key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(x) for x in value if x]
    return []


def _provider_supports(provider: dict, profile_key: str) -> bool:
    required = MODEL_PROFILE_DEFS[profile_key].get("required_capability")
    return not required or required in _normalise_capabilities(provider)


def _resolved_profile_providers(cfg: dict, profile_key: str) -> tuple[list[dict], bool]:
    provider_list = cfg.get("providers", [])
    providers = {p.get("id"): p for p in provider_list}
    explicit_ids = _profile_chain_ids(cfg, profile_key)
    candidates = [providers[pid] for pid in explicit_ids if pid in providers]
    uses_default = not candidates
    if not candidates:
        if profile_key == "private":
            candidates = [p for p in providers.values() if _provider_supports(p, profile_key)]
        else:
            active = providers.get(cfg.get("active_provider_id"))
            candidates = [active] if active else []
    candidates = [p for p in candidates if p and _provider_supports(p, profile_key)]
    # Keep the user's explicit primary and order, then complete short cloud
    # chains with other configured providers. This prevents a single-provider
    # outage from becoming a user-visible failure. Private profiles never gain
    # an implicit cloud fallback.
    if profile_key != "private" and cfg.get("automatic_fallbacks", True):
        selected_ids = {p.get("id") for p in candidates}
        runtime = (_read_runtime().get("providers") or {})
        compatible = [
            p for p in provider_list
            if p.get("id") not in selected_ids
            and p.get("model")
            and _provider_supports(p, profile_key)
        ]
        compatible.sort(key=lambda p: (
            profile_key != "vision" and "视觉" in str(p.get("name") or ""),
            runtime.get(p.get("id"), {}).get("status") == "degraded",
            int(runtime.get(p.get("id"), {}).get("consecutive_failures") or 0),
            int(runtime.get(p.get("id"), {}).get("last_latency_ms") or 0),
        ))
        candidates.extend(compatible[:max(0, 3 - len(candidates))])
    return candidates[:3], uses_default


def list_model_profiles() -> list[dict]:
    """Return logical capability profiles with their effective provider/model."""
    cfg = load_config()
    result = []
    for key, definition in MODEL_PROFILE_DEFS.items():
        chain, uses_default = _resolved_profile_providers(cfg, key)
        provider = chain[0] if chain else None
        result.append({
            "key": key,
            **definition,
            "provider_id": (_profile_chain_ids(cfg, key) or [None])[0],
            "provider_ids": _profile_chain_ids(cfg, key),
            "resolved_provider_id": provider.get("id") if provider else None,
            "provider_name": provider.get("name") if provider else None,
            "model": provider.get("model") if provider else None,
            "chain": [{"id": p.get("id"), "name": p.get("name"), "model": p.get("model")} for p in chain],
            "uses_fallback": uses_default,
            "available": bool(provider and provider.get("model")),
        })
    return result


def set_model_profile(profile_key: str, provider_id: str | None):
    set_model_profile_chain(profile_key, [provider_id] if provider_id else [])


def set_model_profile_chain(profile_key: str, provider_ids: list[str] | None):
    if profile_key not in MODEL_PROFILE_DEFS:
        raise AIError("未知的模型能力档案")
    cfg = load_config()
    provider_ids = list(dict.fromkeys(pid for pid in (provider_ids or []) if pid))[:3]
    providers = {p.get("id"): p for p in cfg.get("providers", [])}
    if any(pid not in providers for pid in provider_ids):
        raise AIError("选择的 AI 供应商不存在")
    incompatible = [providers[pid].get("name") for pid in provider_ids
                    if not _provider_supports(providers[pid], profile_key)]
    if incompatible:
        raise AIError(f"供应商能力不匹配：{'、'.join(incompatible)}")
    bindings = cfg.setdefault("model_profiles", {})
    if provider_ids:
        bindings[profile_key] = provider_ids
    else:
        bindings.pop(profile_key, None)
    save_config(cfg)


def resolve_model_profile(profile_key: str | None) -> dict:
    """Resolve a logical profile to a provider, falling back to the active provider."""
    key = profile_key if profile_key in MODEL_PROFILE_DEFS else "deep_reasoning"
    cfg = load_config()
    chain, uses_default = _resolved_profile_providers(cfg, key)
    provider = chain[0] if chain else None
    if key == "private" and not provider:
        raise AIError("本地隐私档案尚未配置本地模型；为保护资料，Caddie 不会自动发送到云端。")
    provider = _provider_or_raise(provider)
    runtime_provider = dict(provider)
    runtime_provider["_profile_key"] = key
    runtime_provider["_fallback_position"] = 0
    runtime_provider["_fallback_providers"] = [
        {**dict(p), "_profile_key": key, "_fallback_position": index}
        for index, p in enumerate(chain[1:], start=1)
    ]
    return {
        "profile_key": key,
        "profile_name": MODEL_PROFILE_DEFS[key]["name"],
        "provider": runtime_provider,
        "provider_id": provider.get("id"),
        "provider_name": provider.get("name"),
        "model": provider.get("model"),
        "uses_fallback": uses_default,
        "chain": [{"id": p.get("id"), "name": p.get("name"), "model": p.get("model")} for p in chain],
    }


# ─── 统一调用入口 ─────────────────────────────────────────────────────────────

class AIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _friendly_http_error(provider: dict, response) -> AIError:
    """Translate provider payloads into short, actionable product messages."""
    status = response.status_code
    error_type = ""
    raw_message = ""
    try:
        payload = response.json()
        detail = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(detail, dict):
            error_type = str(detail.get("type") or detail.get("code") or "").lower()
            raw_message = str(detail.get("message") or "")
        else:
            raw_message = str(detail)
    except Exception:
        raw_message = (response.text or "")[:240]

    provider_name = provider.get("name") or "当前模型"
    identity = f"{error_type} {raw_message}".lower()
    if status == 429 and any(token in identity for token in ("insufficient_quota", "quota", "billing", "余额")):
        return AIError(
            f"{provider_name} 的 API 额度不足。请充值 API 账户，或在「设置 → 模型能力」中配置其他模型作为备用。",
            status_code=status,
        )
    if status == 429:
        return AIError(
            f"{provider_name} 当前请求过于频繁，请稍后重试；也可以切换备用模型。",
            status_code=status, retryable=True,
        )
    if status in (401, 403):
        return AIError(
            f"{provider_name} 鉴权失败，请检查 API Key 是否正确、有效并拥有该模型权限。",
            status_code=status,
        )
    if status == 404:
        return AIError(
            f"{provider_name} 的接口地址或模型名称不正确，请检查 Base URL 和模型配置。",
            status_code=status,
        )
    if status in (400, 422):
        return AIError(
            f"{provider_name} 拒绝了本次请求，请检查模型名称和接口兼容性。",
            status_code=status,
        )
    if status in (500, 502, 503, 504):
        return AIError(
            f"{provider_name} 服务暂时不可用，Caddie 将尝试备用模型。",
            status_code=status, retryable=True,
        )
    return AIError(
        f"{provider_name} 调用失败（HTTP {status}）。请检查供应商配置或稍后重试。",
        status_code=status,
    )


def _provider_or_raise(provider: dict | None) -> dict:
    provider = provider or get_active_provider()
    if not provider:
        raise AIError("还没有配置 AI 供应商，请到「设置」里添加并选用一个。")
    if not provider.get("model"):
        raise AIError(f"供应商「{provider.get('name')}」还没有选模型。")
    is_local = provider.get("data_policy") == "local" or provider.get("type") == "ollama" or "localhost" in (provider.get("base_url") or "")
    if not is_local and not provider.get("api_key"):
        raise AIError(f"供应商「{provider.get('name')}」还没有填 API Key。")
    return provider


def _decode_provider_json(provider: dict, response) -> dict:
    """Decode a successful provider response without leaking raw JSON errors.

    Some OpenAI-compatible gateways occasionally answer HTTP 200 with an empty
    body or an HTML proxy page. Treat that as a transient provider failure so
    the normal retry/fallback chain can recover instead of surfacing
    ``JSONDecodeError`` to the user.
    """
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        body = str(getattr(response, "text", "") or "").strip()
        detail = "空响应" if not body else "非 JSON 响应"
        raise AIError(
            f"{provider.get('name') or '当前模型'} 接口返回{detail}，Caddie 将重试或切换备用模型。",
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise AIError(
            f"{provider.get('name') or '当前模型'} 接口返回格式异常，Caddie 将重试或切换备用模型。",
            retryable=True,
        )
    return payload


def _should_retry_provider(exc: Exception) -> bool:
    """Retry one transient failure, but never immediately retry a rate limit."""
    if not isinstance(exc, AIError) or not exc.retryable or exc.status_code == 429:
        return False
    # An empty final answer after consuming reasoning tokens is a model/output
    # mode mismatch. Repeating the same expensive request usually produces the
    # same result, so move to the next configured model immediately.
    return "未返回最终回复" not in str(exc)


def chat(messages: list, system: str = "", max_tokens: int = 2048,
         provider: dict | None = None, timeout: int = 35,
         profile_key: str | None = None,
         allow_fallback: bool = True) -> str:
    """统一对话接口，根据能力档案路由，并在失败时尝试该档案的备用模型。
    messages: [{"role": "user"/"assistant", "content": "..."}]
    返回助手回复文本。
    """
    _consume_public_demo_quota()
    primary = provider
    if primary is None:
        primary = resolve_model_profile(profile_key or "deep_reasoning")["provider"]
    if not primary:
        raise AIError("还没有配置 AI 供应商，请到「设置」里添加并选用一个。")
    _LAST_CALL_INFO.set(None)
    candidates = [primary]
    if allow_fallback:
        candidates += list(primary.get("_fallback_providers") or [])
    # Do not make every user wait for a provider that just failed. Keep the
    # configured chain, but temporarily let a healthy fallback go first.
    runtime_health = (_read_runtime().get("providers") or {})
    if len(candidates) > 1:
        primary_health = runtime_health.get(primary.get("id"), {})
        checked_at = primary_health.get("last_checked_at")
        recently_failed = (
            primary_health.get("status") == "degraded"
            or int(primary_health.get("last_latency_ms") or 0) > 25000
        )
        if checked_at:
            try:
                recently_failed = recently_failed and (
                    datetime.now() - datetime.fromisoformat(checked_at)
                ).total_seconds() < 600
            except (TypeError, ValueError):
                pass
        if recently_failed:
            healthy_fallbacks = [
                item for item in candidates[1:]
                if runtime_health.get(item.get("id"), {}).get("status") != "degraded"
            ]
            if healthy_fallbacks:
                first = healthy_fallbacks[0]
                candidates = [first] + [item for item in candidates if item is not first]
    errors = []
    for candidate in candidates:
        started = time.monotonic()
        p = candidate
        for attempt in range(2):
            try:
                p = _provider_or_raise(candidate)
                text, usage = _call_provider(p, messages, system, max_tokens, timeout)
                latency_ms = int((time.monotonic() - started) * 1000)
                _record_call(
                    p, status="success", latency_ms=latency_ms, usage=usage,
                    retry_count=attempt,
                )
                _LAST_CALL_INFO.set({
                    "provider_id": p.get("id"), "provider_name": p.get("name"),
                    "model": p.get("model"), "profile_key": p.get("_profile_key"),
                    "fallback_position": int(p.get("_fallback_position") or 0),
                    "latency_ms": latency_ms, "usage": usage or {},
                })
                return text
            except Exception as exc:
                if attempt == 0 and _should_retry_provider(exc):
                    time.sleep(PROVIDER_RETRY_DELAY_SECONDS)
                    continue
                message = str(exc)
                _record_call(
                    p, status="failed",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=message,
                    retry_count=attempt,
                )
                errors.append(message)
                break
    unique_errors = list(dict.fromkeys(errors))
    if len(unique_errors) == 1:
        raise AIError(unique_errors[0])
    raise AIError("当前模型调用失败：" + "；".join(unique_errors))


def _call_provider(p, messages, system, max_tokens, timeout) -> tuple[str, dict]:
    try:
        if p["type"] == "anthropic":
            return _chat_anthropic(p, messages, system, max_tokens, timeout)
        return _chat_openai(p, messages, system, max_tokens, timeout)
    except requests.exceptions.RequestException as exc:
        raise AIError(f"{p.get('name') or '当前模型'} 网络连接失败：{str(exc)[:160]}", retryable=True)


def _chat_anthropic(p, messages, system, max_tokens, timeout) -> tuple[str, dict]:
    url = (p["base_url"] or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": p.get("api_key", ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    normalized = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "image":
                    parts.append({"type": "image", "source": {
                        "type": "base64", "media_type": part.get("media_type"), "data": part.get("data")
                    }})
                else:
                    parts.append({"type": "text", "text": part.get("text") or ""})
            content = parts
        normalized.append({"role": message.get("role"), "content": content})
    payload = {"model": p["model"], "max_tokens": max_tokens, "messages": normalized}
    if system:
        payload["system"] = system
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise _friendly_http_error(p, r)
    data = _decode_provider_json(p, r)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip(), data.get("usage") or {}


def _chat_openai(p, messages, system, max_tokens, timeout) -> tuple[str, dict]:
    url = (p["base_url"] or "").rstrip("/") + "/chat/completions"
    normalized = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "image":
                    parts.append({"type": "image_url", "image_url": {
                        "url": f"data:{part.get('media_type')};base64,{part.get('data')}",
                        "detail": part.get("detail") or "auto",
                    }})
                else:
                    parts.append({"type": "text", "text": part.get("text") or ""})
            content = parts
        normalized.append({"role": message.get("role"), "content": content})
    msgs = ([{"role": "system", "content": system}] if system else []) + normalized
    headers = {
        "Authorization": f"Bearer {_provider_api_key(p) or 'ollama'}",
        "Content-Type": "application/json",
    }
    payload = {"model": p["model"], "messages": msgs, "max_tokens": max_tokens}
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise _friendly_http_error(p, r)
    data = _decode_provider_json(p, r)
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError):
        content = None
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    text = str(content or "").strip()
    if not text:
        usage = data.get("usage") or {}
        reasoning_tokens = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        detail = "模型把输出额度全部用于内部推理" if reasoning_tokens else "接口返回了空内容"
        raise AIError(
            f"{p.get('name') or '当前模型'} 未返回最终回复（{detail}）。请重试或改用备用模型。",
            retryable=True,
        )
    return text, data.get("usage") or {}


def extract_json(text: str):
    """从模型回复里抠出 JSON（容忍 ```json 包裹和前后废话）。"""
    import json, re
    t = (text or "").strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
        if m:
            t = m.group(1).strip()
    # 退而求其次：截取第一个 { 到最后一个 }
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j != -1:
            t = t[i:j + 1]
    return json.loads(t)


def test_connection(provider: dict) -> dict:
    """用一条极短的 ping 验证供应商可用。"""
    try:
        reply = chat(
            [{"role": "user", "content": "ping，请只回复 ok"}],
            max_tokens=512, provider=provider, timeout=60,
        )
        return {"ok": True, "message": f"连接成功 ✓ 模型回复：{reply[:40]}"}
    except AIError as e:
        return {"ok": False, "message": str(e)}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "message": f"网络错误：{str(e)[:200]}"}
    except Exception as e:
        return {"ok": False, "message": f"失败：{str(e)[:200]}"}
