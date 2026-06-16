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
import uuid
from pathlib import Path

import requests

CONFIG_DIR = Path.home() / ".caddie"
CONFIG_PATH = CONFIG_DIR / "config.json"

# ─── 预设供应商（GUI 里一键填充 base_url 和常用模型）──────────────────────────
# type: "anthropic" 走 /v1/messages，"openai" 走 /chat/completions（OpenAI 兼容）
PRESETS = [
    {
        "key": "anthropic", "name": "Claude (Anthropic)", "type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        "key_hint": "console.anthropic.com 获取，sk-ant- 开头",
    },
    {
        "key": "openai", "name": "OpenAI", "type": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "key_hint": "platform.openai.com 获取，sk- 开头",
    },
    {
        "key": "deepseek", "name": "DeepSeek", "type": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_hint": "platform.deepseek.com 获取",
    },
    {
        "key": "moonshot", "name": "Kimi (Moonshot)", "type": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "key_hint": "platform.moonshot.cn 获取",
    },
    {
        "key": "zhipu", "name": "智谱 GLM", "type": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash"],
        "key_hint": "bigmodel.cn 获取",
    },
    {
        "key": "siliconflow", "name": "硅基流动 SiliconFlow", "type": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "key_hint": "siliconflow.cn 获取（聚合多模型）",
    },
    {
        "key": "ollama", "name": "本地 Ollama", "type": "openai",
        "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5", "llama3.1", "deepseek-r1"],
        "key_hint": "本地运行，api_key 可留空填 ollama",
    },
    {
        "key": "custom", "name": "自定义（OpenAI 兼容）", "type": "openai",
        "base_url": "",
        "models": [],
        "key_hint": "任何兼容 OpenAI /chat/completions 的服务",
    },
]


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


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return key[:6] + "···" + key[-4:]


def list_providers_masked() -> dict:
    """给前端用：api_key 打码。"""
    cfg = load_config()
    providers = []
    for p in cfg.get("providers", []):
        q = dict(p)
        q["api_key_masked"] = _mask(p.get("api_key", ""))
        q["has_key"] = bool(p.get("api_key"))
        q.pop("api_key", None)
        providers.append(q)
    return {
        "providers": providers,
        "active_provider_id": cfg.get("active_provider_id"),
        "presets": PRESETS,
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
    }

    found = False
    for i, p in enumerate(providers):
        if p["id"] == record["id"]:
            if not incoming_key:          # 没传新 key → 保留旧的
                record["api_key"] = p.get("api_key", "")
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


# ─── 统一调用入口 ─────────────────────────────────────────────────────────────

class AIError(Exception):
    pass


def _provider_or_raise(provider: dict | None) -> dict:
    provider = provider or get_active_provider()
    if not provider:
        raise AIError("还没有配置 AI 供应商，请到「设置」里添加并选用一个。")
    if not provider.get("model"):
        raise AIError(f"供应商「{provider.get('name')}」还没有选模型。")
    if provider.get("type") != "ollama" and not provider.get("api_key") and provider.get("type") == "anthropic":
        raise AIError(f"供应商「{provider.get('name')}」还没有填 API Key。")
    return provider


def chat(messages: list, system: str = "", max_tokens: int = 2048,
         provider: dict | None = None, timeout: int = 120) -> str:
    """统一对话接口，根据 active provider 的 type 路由到对应 API。
    messages: [{"role": "user"/"assistant", "content": "..."}]
    返回助手回复文本。
    """
    p = _provider_or_raise(provider)
    if p["type"] == "anthropic":
        return _chat_anthropic(p, messages, system, max_tokens, timeout)
    return _chat_openai(p, messages, system, max_tokens, timeout)


def _chat_anthropic(p, messages, system, max_tokens, timeout) -> str:
    url = (p["base_url"] or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    headers = {
        "x-api-key": p.get("api_key", ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {"model": p["model"], "max_tokens": max_tokens, "messages": messages}
    if system:
        payload["system"] = system
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise AIError(f"Claude 接口错误 {r.status_code}：{r.text[:300]}")
    data = r.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _chat_openai(p, messages, system, max_tokens, timeout) -> str:
    url = (p["base_url"] or "").rstrip("/") + "/chat/completions"
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    headers = {
        "Authorization": f"Bearer {p.get('api_key') or 'ollama'}",
        "Content-Type": "application/json",
    }
    payload = {"model": p["model"], "messages": msgs, "max_tokens": max_tokens}
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if r.status_code != 200:
        raise AIError(f"接口错误 {r.status_code}：{r.text[:300]}")
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


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
            max_tokens=16, provider=provider, timeout=30,
        )
        return {"ok": True, "message": f"连接成功 ✓ 模型回复：{reply[:40]}"}
    except AIError as e:
        return {"ok": False, "message": str(e)}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "message": f"网络错误：{str(e)[:200]}"}
    except Exception as e:
        return {"ok": False, "message": f"失败：{str(e)[:200]}"}
