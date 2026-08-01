"""Regression test for unbound knowledge-coach creation and confirmation."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import ai
import db
import server
import vcs


def main():
    original_chat = ai.chat
    original_selection = server._expert_model_selection
    with TemporaryDirectory(prefix="caddie-global-knowledge-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None
        server._expert_model_selection = lambda _expert: {
            "profile_key": "writing", "provider_id": "test",
            "model": "test-model", "profile_name": "测试写作",
            "provider_name": "测试模型", "uses_fallback": False,
            "provider": {"id": "test", "name": "测试模型", "model": "test-model"},
        }
        ai.chat = lambda *args, **kwargs: (
            "SUMMARY: 已整理成可复用知识\n"
            "TITLE: 债券发行承销基础\n"
            "===DOCUMENT===\n"
            "# 债券发行承销基础\n\n"
            "## 核心流程\n\n从项目准备、申报到簿记发行形成完整链路。\n\n"
            "## 与个人经历的连接\n\n当前资料不足的部分明确标记为待补充。"
        )

        with TestClient(server.app) as client:
            task_id = db.create_agent_task({
                "task_type": "knowledge",
                "title": "学习债券承销",
                "instruction": "整理一份债券发行承销知识文档并保存",
                "assigned_expert": "knowledge_coach",
                "status": "queued",
            })
            server._run_agent_task(task_id)
            task = client.get(f"/api/agent/tasks/{task_id}")
            assert task.status_code == 200, task.text
            payload = task.json()
            assert payload["status"] == "review", payload
            change = payload["changes"][0]
            assert change["action_type"] == "create_global_knowledge"
            assert not db.list_knowledge_items(scope_type="global")

            applied = client.post(
                f"/api/agent/tasks/{task_id}/apply",
                json={"change_ids": [change["id"]]},
            )
            assert applied.status_code == 200, applied.text
            items = db.list_knowledge_items(scope_type="global")
            assert len(items) == 1 and items[0]["title"] == "债券发行承销基础"
            refreshed = client.get(f"/api/agent/tasks/{task_id}").json()
            assert any(
                item["type"] == "knowledge_item" and item["id"] == items[0]["id"]
                for item in refreshed["artifacts"]
            )

    ai.chat = original_chat
    server._expert_model_selection = original_selection
    print("AGENT_GLOBAL_KNOWLEDGE_OK")


if __name__ == "__main__":
    main()
