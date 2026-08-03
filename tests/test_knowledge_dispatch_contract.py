"""Acceptance coverage for model-driven knowledge dispatch and references."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
import db
import server
import vcs


def selection(_expert):
    return {"profile_key": "writing", "provider_id": "test", "model": "test",
            "profile_name": "test", "provider_name": "test", "uses_fallback": False,
            "provider": {"id": "test", "model": "test"}}


def dispatch_payload(intent, plan, topics=None, clarification=False, question=None):
    return {"intent": intent, "references": [], "resolved_topics": topics or [],
            "operations": [intent], "output": {"type": "knowledge_document", "count": len(plan)},
            "document_plan": plan, "needs_clarification": clarification,
            "confidence": .95 if not clarification else .45, "evidence": ["结合最近对话与当前候选"],
            "clarification_question": question}


def main():
    original_chat, original_selection = server.ai.chat, server._expert_model_selection
    with TemporaryDirectory(prefix="caddie-dispatch-") as temp:
        root = Path(temp); db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"; vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None; server._expert_model_selection = selection
        mode = {"name": "create"}; current_ids = {"ids": []}; repair_calls = {"count": 0}

        def fake_chat(messages, **kwargs):
            system = kwargs.get("system") or ""
            if mode["name"] == "service_error":
                raise RuntimeError("provider connection timeout")
            if "分诊结果修复" in system:
                repair_calls["count"] += 1
                return json.dumps(dispatch_payload("create_multiple_documents", [
                    {"title": f"{topic} 核心知识", "goal": f"深入解释 {topic}", "action": "create"}
                    for topic in ["Skill", "API", "CLI"]
                ], ["Skill", "API", "CLI"]), ensure_ascii=False)
            if "输入分诊器" in system:
                if mode["name"] == "broken_json":
                    return '{"intent":"create_multiple_documents","resolved_topics":["Skill","API","CLI"]'
                if mode["name"] == "create":
                    return json.dumps(dispatch_payload("create_multiple_documents", [
                        {"title": "Skill 核心知识", "goal": "详细解释 Skill", "action": "create"},
                        {"title": "API 核心知识", "goal": "详细解释 API", "action": "create"},
                        {"title": "CLI 核心知识", "goal": "详细解释 CLI", "action": "create"},
                    ], ["Skill", "API", "CLI"]), ensure_ascii=False)
                if mode["name"] == "edit_api":
                    return json.dumps(dispatch_payload("edit_document", [{
                        "title": "API 核心知识", "goal": "改成产品经理面试表达", "action": "revise_candidate",
                        "source_candidate_ids": [current_ids["ids"][1]],
                    }], ["API"]), ensure_ascii=False)
                if mode["name"] == "merge":
                    return json.dumps(dispatch_payload("merge_documents", [
                        {"title": "Skill 与 API", "goal": "合并两篇并消除重复", "action": "revise_candidate", "source_candidate_ids": current_ids["ids"][:2]},
                        {"title": "CLI 核心知识", "goal": "CLI 保持独立", "action": "revise_candidate", "source_candidate_ids": [current_ids["ids"][2]]},
                    ], ["Skill", "API", "CLI"]), ensure_ascii=False)
                if mode["name"] == "ambiguous":
                    return json.dumps(dispatch_payload("clarify_topic", [], clarification=True,
                        question="你指的是哪一组中的第二篇？请选择文档标题。"), ensure_ascii=False)
                if mode["name"] == "continue":
                    return json.dumps(dispatch_payload("continue_document", [], ["Skill", "API", "CLI"]), ensure_ascii=False)
                if mode["name"] == "deepen_candidates":
                    return json.dumps(dispatch_payload("edit_document", [
                        {"title": "CLI 概念深入解析", "goal": "加深 CLI 候选", "action": "update_document", "target_knowledge_item_id": None},
                        {"title": "Skill 概念深入解析", "goal": "加深 Skill 候选", "action": "update_document", "target_knowledge_item_id": None},
                    ], ["CLI", "Skill"]), ensure_ascii=False)
                return json.dumps(dispatch_payload("edit_document", [
                    {"title": title, "goal": "增加真实办公场景案例", "action": "revise_candidate", "source_candidate_ids": [cid]}
                    for title, cid in zip(["Skill 核心知识", "API 核心知识", "CLI 核心知识"], current_ids["ids"])
                ], ["Skill", "API", "CLI"]), ensure_ascii=False)
            material = messages[0]["content"]
            title = next(line.removeprefix("【本篇标题】") for line in material.splitlines() if line.startswith("【本篇标题】"))
            document="完整原理、产品判断与办公案例。"*100
            return f"SUMMARY: 已完成\nTITLE: {title}\n===DOCUMENT===\n# {title}\n\n{document}"

        server.ai.chat = fake_chat
        with TestClient(server.app) as client:
            track_id = db.create_job_track({"company": "百度", "role": "AI产品经理"})
            session = db.resolve_workspace_session(f"knowledge_coach:track:{track_id}:new",
                mode="knowledge_coach", title="新建知识准备", track_id=track_id)
            sid = session["id"]
            db.save_message(sid, "assistant", "需要学习 Skill、API、CLI 三个概念。")

            # 1. Reference previous turn and create three independently persisted candidates.
            routed = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "三个概念再详细一点，每个概念都建立一个文档"}).json()
            assert routed["dispatch"]["resolved_topics"] == ["Skill", "API", "CLI"]
            assert not routed["dispatch"]["needs_clarification"]
            server._run_agent_task(routed["task"]["id"])
            created = db.get_agent_task(routed["task"]["id"])
            assert len(created["changes"]) == 3 and all(x["status"] == "pending" for x in created["changes"])
            current_ids["ids"] = [x["id"] for x in created["changes"]]
            for change_id in current_ids["ids"][1:]:
                db.edit_proposed_change(change_id, proposed_content="过于简短")

            # Continuing resumes the existing plan instead of creating duplicate documents.
            mode["name"] = "continue"
            continued = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "继续处理"}).json()
            assert continued["resume_task_id"] == routed["task"]["id"]
            assert not continued["already_ready"] and len(continued["retry_change_ids"]) == 2
            assert continued["pending_count"] == 1
            assert continued["task"] is None

            # Candidate references win over a stale bound formal document; user is not asked to choose IDs.
            mode["name"] = "deepen_candidates"
            deepened = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "CLI和Skill给的太少了，这两篇再深入一些"}).json()
            assert not deepened["dispatch"]["needs_clarification"]
            assert all(item["action"] == "revise_candidate" for item in deepened["dispatch"]["document_plan"])
            assert [item["source_candidate_ids"] for item in deepened["dispatch"]["document_plan"]] == [[current_ids["ids"][2]], [current_ids["ids"][0]]]

            # 2. Editing API produces one revised candidate, not a fourth document plan item.
            mode["name"] = "edit_api"
            edited = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "API那篇太技术了，改成产品经理面试能讲的。"}).json()
            assert edited["dispatch"]["intent"] == "edit_document"
            assert len(edited["dispatch"]["document_plan"]) == 1
            server._run_agent_task(edited["task"]["id"])
            edited_task = db.get_agent_task(edited["task"]["id"])
            assert len(edited_task["changes"]) == 1, edited_task

            # 3. Merge updates the plan to exactly two outputs.
            mode["name"] = "merge"
            merged = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "Skill和API可以放一起，CLI单独写。"}).json()
            assert len(merged["dispatch"]["document_plan"]) == 2

            # Malformed structured output is repaired automatically instead of blaming user ambiguity.
            mode["name"] = "broken_json"
            repaired = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "深入解析 Skill、API、CLI，形成三个文档。"}).json()
            assert repair_calls["count"] == 1
            assert not repaired["dispatch"]["needs_clarification"]
            assert len(repaired["dispatch"]["document_plan"]) == 3

            # A real service failure is persisted and can be retried with the original input.
            mode["name"] = "service_error"
            failed = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "请继续创建 Skill、API、CLI 三篇文档"}).json()
            assert failed["dispatch"]["technical_failure"]
            assert failed["dispatch"]["failure_kind"] == "timeout"
            failure_task_id = failed["dispatch"]["failure_task_id"]
            assert db.get_agent_task(failure_task_id)["status"] == "failed"
            mode["name"] = "create"
            retried = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch/retry",
                json={"failure_task_id": failure_task_id}).json()
            assert retried["task"] and not retried["dispatch"]["technical_failure"]
            assert db.get_agent_task(failure_task_id)["status"] == "completed"

            # 4. Ambiguous ordinal is clarification-only and creates no task.
            mode["name"] = "ambiguous"
            ambiguous = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "第二篇再详细一点。"}).json()
            assert ambiguous["dispatch"]["needs_clarification"] and ambiguous["task"] is None

            # 6. Batch adjustment resolves to three candidate revisions, not three new topics.
            mode["name"] = "batch"
            batch = client.post(f"/api/workspace-sessions/{sid}/knowledge-dispatch",
                json={"message": "三篇都增加一个真实办公场景案例。"}).json()
            assert batch["dispatch"]["intent"] == "edit_document"
            assert len(batch["dispatch"]["document_plan"]) == 3
            assert all(x["action"] == "revise_candidate" for x in batch["dispatch"]["document_plan"])

    server.ai.chat, server._expert_model_selection = original_chat, original_selection
    print("KNOWLEDGE_DISPATCH_CONTRACT_OK")


if __name__ == "__main__":
    main()
