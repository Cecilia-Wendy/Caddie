"""Multi-document knowledge planning, partial failure, and targeted retry."""
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


def selection(_expert):
    return {
        "profile_key": "writing", "provider_id": "test", "model": "test-model",
        "profile_name": "测试写作", "provider_name": "测试模型",
        "uses_fallback": False, "provider": {"id": "test", "model": "test-model"},
    }


def main():
    original_chat = server.ai.chat
    original_selection = server._expert_model_selection
    with TemporaryDirectory(prefix="caddie-multi-knowledge-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None
        server._expert_model_selection = selection
        db.init_db()

        track_id = db.create_job_track({"company": "百度", "role": "AI产品经理"})
        session = db.resolve_workspace_session(
            f"knowledge_coach:track:{track_id}:new", mode="knowledge_coach",
            title="Skill、API 与 CLI 知识准备", track_id=track_id,
        )
        task_id = db.create_agent_task({
            "task_type": "knowledge_create", "title": "Skill、API 与 CLI 知识准备",
            "instruction": "把 Skill、API、CLI 分别详细整理，每个概念建立一个文档",
            "object_type": "job_track", "object_id": track_id, "track_id": track_id,
            "assigned_expert": "knowledge_coach", "conversation_id": session["id"],
            "status": "queued", "context_json": json.dumps({"folder_name": "专业知识", "dispatch": {
                "intent": "create_multiple_documents", "resolved_topics": ["Skill", "API", "CLI"],
                "document_plan": [
                    {"title": "Skill 基础", "goal": "讲清 Skill 定义、生态与设计规范", "action": "create"},
                    {"title": "API 基础", "goal": "讲清 API、鉴权与治理", "action": "create"},
                    {"title": "CLI 基础", "goal": "讲清 CLI、安全边界与错误恢复", "action": "create"},
                ],
            }}),
        })

        retrying = {"api": False}; quality_retries = {"count": 0}

        def fake_chat(messages, **kwargs):
            system = kwargs.get("system") or ""
            if "拆成 2-5 篇" in system:
                return json.dumps({"documents": [
                    {"title": "Skill 基础", "goal": "讲清 Skill 定义、生态与设计规范"},
                    {"title": "API 基础", "goal": "讲清 API、鉴权与治理"},
                    {"title": "CLI 基础", "goal": "讲清 CLI、安全边界与错误恢复"},
                ]}, ensure_ascii=False)
            material = messages[0]["content"]
            if "【本篇标题】API 基础" in material and not retrying["api"]:
                raise RuntimeError("provider unavailable")
            title = next(
                line.removeprefix("【本篇标题】")
                for line in material.splitlines() if line.startswith("【本篇标题】")
            )
            if title == "Skill 基础":
                document = f"# {title}\n\n" + "深入原理与产品案例。" * 180
            elif "【质量校验】" in material:
                quality_retries["count"] += 1
                document = f"# {title}\n\n" + "深入原理、产品判断、办公案例与面试表达。" * 100
            else:
                document = f"# {title}\n\n过于简短"
            return f"SUMMARY: 已完成 {title}\nTITLE: {title}\n===DOCUMENT===\n{document}"

        server.ai.chat = fake_chat
        server._run_agent_task(task_id)
        task = db.get_agent_task(task_id)
        assert task["status"] == "review", task
        assert [item["status"] for item in task["changes"]] == ["pending", "failed", "pending"]
        assert [item["proposed_title"] for item in task["changes"]] == [
            "Skill 基础", "API 基础", "CLI 基础",
        ]
        assert quality_retries["count"] == 1
        assert len(task["changes"][2]["proposed_content"]) >= len(task["changes"][0]["proposed_content"]) * .55
        assert not db.list_knowledge_items(scope_type="track", track_id=track_id)

        retrying["api"] = True
        failed_id = task["changes"][1]["id"]
        retried = server.retry_failed_knowledge_items(
            task_id, server.AgentTaskRetryFailedIn(change_ids=[failed_id]),
        )
        assert [item["status"] for item in retried["changes"]] == ["pending", "pending", "pending"]
        assert quality_retries["count"] == 2
        assert retried["changes"][0]["proposed_content"].startswith("# Skill")
        assert retried["changes"][2]["proposed_content"].startswith("# CLI")

        applied_one = server.apply_agent_task(
            task_id, server.AgentTaskApplyIn(change_ids=[retried["changes"][0]["id"]]),
        )
        assert [item["status"] for item in applied_one["task"]["changes"]] == ["applied", "pending", "pending"]
        assert len(db.list_knowledge_items(scope_type="track", track_id=track_id)) == 1

        other_task_id = db.create_agent_task({
            "task_type": "knowledge_create", "title": "另一知识任务",
            "instruction": "生成另一篇文档", "object_type": "job_track",
            "object_id": track_id, "track_id": track_id,
            "assigned_expert": "knowledge_coach", "conversation_id": session["id"],
            "status": "review",
        })
        try:
            server.apply_agent_task(
                other_task_id,
                server.AgentTaskApplyIn(change_ids=[retried["changes"][2]["id"]]),
            )
            raise AssertionError("a candidate from another task must not be applied")
        except server.HTTPException as error:
            assert error.status_code == 409
            assert error.detail == "页面候选已更新，请刷新后再次确认"

        # A restart preserves completed candidates and exposes only the interrupted item for retry.
        interrupted_id = retried["changes"][1]["id"]
        db.update_proposed_change(interrupted_id, "generating")
        db.update_agent_task(task_id, status="active")
        server._recover_knowledge_coach_tasks()
        recovered = db.get_agent_task(task_id)
        assert [item["status"] for item in recovered["changes"]] == ["applied", "failed", "pending"]
        assert "1 篇待确认" in recovered["result_summary"]
        assert "1 篇可单独重试" in recovered["result_summary"]

    server.ai.chat = original_chat
    server._expert_model_selection = original_selection
    print("KNOWLEDGE_COACH_MULTI_DOCUMENT_OK")


if __name__ == "__main__":
    main()
