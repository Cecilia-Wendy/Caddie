"""Regression coverage for the unified knowledge-coach workspace contract."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db
import server
import vcs


def model_selection(_expert):
    return {
        "profile_key": "writing", "provider_id": "test", "model": "test-model",
        "profile_name": "测试写作", "provider_name": "测试模型",
        "uses_fallback": False, "provider": {"id": "test", "model": "test-model"},
    }


def main():
    original_chat = server.ai.chat
    original_selection = server._expert_model_selection
    with TemporaryDirectory(prefix="caddie-knowledge-workspace-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None
        server._expert_model_selection = model_selection
        db.init_db()

        knowledge_id = db.create_knowledge_item({
            "title": "原文", "content": "# 原文\n\n基础内容", "scope_type": "global",
            "topic": "测试", "mastery": "learning", "status": "active",
        })
        seen = {}

        def fake_chat(messages, **_kwargs):
            seen["material"] = messages[0]["content"]
            return (
                "SUMMARY: 已按要求加深案例\nTITLE: 候选第二版\n"
                "===DOCUMENT===\n# 候选第二版\n\n保留上一版候选，并补充案例。"
            )

        server.ai.chat = fake_chat
        task_id = db.create_agent_task({
            "task_type": "knowledge_edit", "title": "继续修改", "instruction": "案例再深入",
            "object_type": "knowledge_item", "object_id": knowledge_id,
            "assigned_expert": "knowledge_coach", "status": "queued",
            "context_json": json.dumps({
                "mode": "knowledge_coach",
                "base_candidate": {"title": "候选第一版", "content": "# 候选第一版\n\n未确认内容"},
            }, ensure_ascii=False),
        })
        server._run_agent_task(task_id)
        task = db.get_agent_task(task_id)
        assert task["status"] == "review", task
        assert "候选第一版" in seen["material"] and "未确认内容" in seen["material"]
        assert db.get_knowledge_item(knowledge_id)["title"] == "原文"

        # A restart must recover pending candidates instead of rerunning or discarding them.
        db.update_agent_task(task_id, status="active")
        server._recover_knowledge_coach_tasks()
        recovered = db.get_agent_task(task_id)
        assert recovered["status"] == "review", recovered
        assert recovered["changes"][0]["status"] == "pending"
        assert db.get_knowledge_item(knowledge_id)["content"] == "# 原文\n\n基础内容"

    server.ai.chat = original_chat
    server._expert_model_selection = original_selection
    print("KNOWLEDGE_COACH_WORKSPACE_OK")


if __name__ == "__main__":
    main()
