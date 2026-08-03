"""Regression test for a project-bound Agent producing a safe document update."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
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
    with TemporaryDirectory(prefix="caddie-project-update-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None
        server._expert_model_selection = lambda _expert: {
            "profile_key": "deep_reasoning", "provider_id": "test",
            "model": "test-model", "profile_name": "测试推理",
            "provider_name": "测试模型", "uses_fallback": False,
            "provider": {"id": "test", "name": "测试模型", "model": "test-model"},
        }
        replies = iter([
            json.dumps({
                "summary": "事实边界已检查",
                "confirmed_facts": ["用户定义了测试口径"],
                "uncertainties": ["结果归因仍需确认"],
                "followups": [{
                    "question": "结果中哪些部分可以归因于你的方案？",
                    "kind": "factual", "category": "结果归因",
                    "reason": "避免扩大个人贡献", "severity": "blocker",
                }],
            }, ensure_ascii=False),
            "SUMMARY: 按事实边界完善项目文档\n"
            "TITLE: 测试口径统一项目\n"
            "===DOCUMENT===\n"
            "# 测试口径统一项目\n\n## 背景\n\n团队需要统一执行口径。\n\n"
            "## 我的动作与关键决策\n\n我定义测试口径并组织评审。\n\n"
            "## 结果与价值\n\n具体归因仍待确认。",
        ])
        ai.chat = lambda *args, **kwargs: next(replies)

        with TestClient(server.app) as client:
            experience_id = client.post("/api/experiences", json={
                "company": "示例公司", "role": "产品实习生",
            }).json()["id"]
            project_id = client.post(f"/api/experiences/{experience_id}/projects", json={
                "name": "测试口径统一项目", "document": "# 原文\n\n已有事实。",
            }).json()["id"]
            task_id = db.create_agent_task({
                "task_type": "experience_discovery",
                "title": "完善项目",
                "instruction": "请检查事实并更新项目文档",
                "object_type": "project", "object_id": project_id,
                "assigned_expert": "experience_detective", "status": "queued",
            })
            server._run_agent_task(task_id)
            task = client.get(f"/api/agent/tasks/{task_id}").json()
            assert task["status"] == "review", task
            actions = {change["action_type"] for change in task["changes"]}
            assert actions == {"create_followup", "update_project_document"}, actions
            update = next(
                change for change in task["changes"]
                if change["action_type"] == "update_project_document"
            )
            assert client.post(
                f"/api/agent/tasks/{task_id}/apply",
                json={"change_ids": [update["id"]]},
            ).status_code == 200
            assert "我的动作与关键决策" in db.get_project(project_id)["document"]

    ai.chat = original_chat
    server._expert_model_selection = original_selection
    print("AGENT_PROJECT_UPDATE_OK")


if __name__ == "__main__":
    main()
