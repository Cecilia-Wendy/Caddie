"""End-to-end contract test for Caddie as an external Agent data plane.

This is intentionally a realistic career-prep flow, not just an endpoint probe:
an external Agent works on a job line, saves a draft, proposes a fact and a
feedback rule, the user confirms them, then edits a project. The Agent can see
that human edit through the incremental change cursor.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import db
import server
import vcs
import caddie_mcp


def main():
    with TemporaryDirectory(prefix="caddie-agent-e2e-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None
        caddie_mcp._commit = lambda _message: None
        future_interview_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d 14:30")

        with TestClient(server.app) as client:
            integrations = client.get("/api/agent/integrations")
            assert integrations.status_code == 200, integrations.text
            assert {x["key"] for x in integrations.json()["clients"]} == {
                "codex", "claude_desktop", "workbuddy",
            }
            for item in integrations.json()["clients"]:
                assert item.get("pricing_summary")
                assert item.get("recommendation")
            assert all(item.get("official_url") and item.get("pricing_url")
                       for item in integrations.json()["clients"])

            # The guided installer must merge rather than replace an existing Agent
            # configuration, and keep a backup before it writes a second time.
            agent_home = root / "agent-home"
            integration_paths = server._agent_integration_paths(agent_home)
            codex_config = integration_paths["codex"]
            codex_config.parent.mkdir(parents=True, exist_ok=True)
            codex_config.write_text('[mcp_servers.other]\ncommand = "/tmp/other.sh"\n', encoding="utf-8")
            merged_codex = server._merge_codex_mcp_config(
                codex_config.read_text(encoding="utf-8"),
                integration_paths["mcp_command"], integration_paths["mcp_args"],
            )
            backup_path = server._integration_backup_and_write(codex_config, merged_codex)
            assert backup_path and Path(backup_path).exists()
            assert "[mcp_servers.other]" in codex_config.read_text(encoding="utf-8")
            assert server._codex_is_configured(
                codex_config, integration_paths["mcp_command"], integration_paths["mcp_args"]
            )

            claude_config = integration_paths["claude_desktop"]
            claude_config.parent.mkdir(parents=True, exist_ok=True)
            claude_config.write_text(json.dumps({"mcpServers": {"other": {"command": "/tmp/other.sh"}}}), encoding="utf-8")
            merged_claude = server._merge_claude_mcp_config(
                claude_config.read_text(encoding="utf-8"),
                integration_paths["mcp_command"], integration_paths["mcp_args"],
            )
            server._integration_backup_and_write(claude_config, merged_claude)
            merged_claude_data = json.loads(claude_config.read_text(encoding="utf-8"))
            assert "other" in merged_claude_data["mcpServers"]
            assert server._claude_is_configured(
                claude_config, integration_paths["mcp_command"], integration_paths["mcp_args"]
            )
            workbuddy_config = integration_paths["workbuddy"]
            workbuddy_config.parent.mkdir(parents=True, exist_ok=True)
            workbuddy_config.write_text(
                json.dumps({"mcpServers": {"other": {"command": "/tmp/other.sh"}}}),
                encoding="utf-8",
            )
            merged_workbuddy = server._merge_json_mcp_config(
                workbuddy_config.read_text(encoding="utf-8"),
                integration_paths["mcp_command"], integration_paths["mcp_args"], "workbuddy",
            )
            server._integration_backup_and_write(workbuddy_config, merged_workbuddy)
            workbuddy_data = json.loads(workbuddy_config.read_text(encoding="utf-8"))
            assert "other" in workbuddy_data["mcpServers"]
            assert workbuddy_data["mcpServers"]["caddie"]["env"]["CADDIE_AGENT_KEY"] == "workbuddy"
            assert server._json_mcp_is_configured(
                workbuddy_config, integration_paths["mcp_command"], integration_paths["mcp_args"]
            )
            experience = client.post("/api/experiences", json={
                "company": "示例自动驾驶公司", "role": "算法产品实习生",
            })
            assert experience.status_code == 200, experience.text
            eid = experience.json()["id"]
            project = client.post(f"/api/experiences/{eid}/projects", json={
                "name": "窄距通行能力摸底", "one_liner": "为降低不必要接管提供场景数据依据",
                "document": (
                    "背景：需要重新定义窄距通行问题。\n我的动作：拆解场景与数据字段。\n"
                    + ("详细面试问答素材，必须仅在按需读取时展开。\n" * 600)
                ),
                "technologies": "Excel, SQL", "keywords": "自动驾驶, 场景测试",
            })
            assert project.status_code == 200, project.text
            project_id = project.json()["id"]
            track = client.post("/api/job-tracks", json={
                "track_group": "AI 产品", "company": "示例公司", "role": "产品经理实习生",
                "target": "", "status": "active", "priority": "high", "jd": "需要产品与数据能力",
            })
            assert track.status_code == 200, track.text
            track_id = track.json()["id"]

            schedule = caddie_mcp.propose_interview_change(
                track_id=track_id, operation="schedule",
                interview_date=future_interview_at, round_type="业务一面",
                meeting_link="https://example.test/meeting",
                reason="验证外部 Agent 可以提交排期，但不能绕过用户确认",
                agent_key="codex-e2e",
            )
            assert not next(x for x in db.list_job_tracks() if x["id"] == track_id).get("next_interview_at")
            schedule_apply = client.post(
                f"/api/agent/tasks/{schedule['task_id']}/apply",
                json={"change_ids": [schedule["change_id"]]},
            )
            assert schedule_apply.status_code == 200, schedule_apply.text
            scheduled_track = next(x for x in db.list_job_tracks() if x["id"] == track_id)
            assert scheduled_track["next_interview_at"] == future_interview_at
            interview_id = schedule_apply.json()["results"][0]["interview_id"]

            remove_schedule = caddie_mcp.propose_interview_change(
                track_id=track_id, operation="delete", interview_id=interview_id,
                reason="验证错误挂载的面试可经确认删除", agent_key="codex-e2e",
            )
            remove_apply = client.post(
                f"/api/agent/tasks/{remove_schedule['task_id']}/apply",
                json={"change_ids": [remove_schedule["change_id"]]},
            )
            assert remove_apply.status_code == 200, remove_apply.text
            assert db.get_interview(interview_id) is None

            record_change = caddie_mcp.propose_job_record_update(
                track_id=track_id,
                changes={"priority": "normal", "application_status": "screening",
                         "remark_tag": "Agent 提议，用户确认"},
                reason="验证岗位事务字段的受控修改", agent_key="codex-e2e",
            )
            assert db.get_application(scheduled_track["application_id"])["status"] == "interview"
            record_apply = client.post(
                f"/api/agent/tasks/{record_change['task_id']}/apply",
                json={"change_ids": [record_change["change_id"]]},
            )
            assert record_apply.status_code == 200, record_apply.text
            updated_application = db.get_application(scheduled_track["application_id"])
            assert updated_application["status"] == "screening"
            assert updated_application["remark_tag"] == "Agent 提议，用户确认"

            workspace_map = caddie_mcp.get_workspace_map(track_id=track_id)
            assert workspace_map["scope"] == "job"
            assert {x["value"] for x in workspace_map["write_destinations"]} >= {
                "job.interview", "job.professional", "draft", "existing_document",
            }
            high_level = caddie_mcp.save_career_document(
                title="高层工具生成的面试清单",
                body="# 面试清单\n\n这是等待用户确认的候选。",
                destination="job.interview", track_id=track_id,
                reason="验证 Agent 无需手工创建任务即可写到正确位置",
                agent_key="codex-e2e",
            )
            assert high_level["requires_confirmation"] is True
            assert high_level["destination"]["folder_name"] == "面试准备"
            assert not [x for x in db.list_knowledge_items(track_id=track_id)
                        if x.get("title") == "高层工具生成的面试清单"]
            high_level_apply = client.post(
                f"/api/agent/tasks/{high_level['task_id']}/apply",
                json={"change_ids": [high_level["change_id"]]},
            )
            assert high_level_apply.status_code == 200, high_level_apply.text
            high_level_doc = next(
                x for x in db.list_knowledge_items(track_id=track_id)
                if x.get("title") == "高层工具生成的面试清单"
            )
            assert db.get_knowledge_folder(high_level_doc["folder_id"])["name"] == "面试准备"

            boot = client.post("/api/agent/workspace-bootstrap", json={"agent_key": "codex-e2e", "track_id": track_id})
            assert boot.status_code == 200, boot.text
            cursor = boot.json()["workspace"]["change_cursor"]

            run = client.post("/api/agent/external-runs", json={
                "title": "准备 AI 产品岗位项目表达", "instruction": "为面试准备项目表达与事实核验",
                "agent_key": "codex-e2e", "track_id": track_id,
                "object_type": "project", "object_id": project_id,
            })
            assert run.status_code == 200, run.text
            task_id = run.json()["task"]["id"]

            draft = client.post("/api/agent/draft-assets", json={
                "title": "窄距通行项目 90 秒表达（草稿）", "body": "我先定义问题，再统一测试口径，最后用数据调整后续方向。",
                "asset_type": "project_pitch", "track_id": track_id, "project_id": project_id,
                "task_id": task_id, "agent_key": "codex-e2e",
            })
            assert draft.status_code == 200, draft.text
            fact = client.post("/api/agent/proposals/fact", json={
                "task_id": task_id, "subject_type": "project", "subject_id": project_id,
                "scope_type": "project", "scope_id": project_id, "predicate": "scenario_count",
                "value_text": "覆盖 7 个高频窄距场景", "confidence": 0.9,
                "evidence": [{"type": "project_note", "ref": "测试计划"}], "agent_key": "codex-e2e",
            })
            assert fact.status_code == 200, fact.text
            fact_change_id = fact.json()["change_id"]
            feedback = client.post("/api/agent/proposals/feedback", json={
                "task_id": task_id, "original_text": "不要把团队测试结论写成我个人独立完成。",
                "scope": "project", "scope_id": project_id, "strength": "hard", "agent_key": "codex-e2e",
            })
            assert feedback.status_code == 200, feedback.text

            edited_fact = client.patch(f"/api/agent/tasks/{task_id}/changes/{fact_change_id}", json={
                "proposed_title": "scenario_count", "proposed_content": "覆盖 7 个高频窄距场景（用户确认措辞）",
                "reason": "保留量化范围，同时明确这是项目覆盖范围。",
            })
            assert edited_fact.status_code == 200, edited_fact.text
            assert next(x for x in edited_fact.json()["task"]["changes"] if x["id"] == fact_change_id)["proposed_content"].endswith("用户确认措辞）")

            rejected = client.post("/api/agent/proposals/fact", json={
                "task_id": task_id, "subject_type": "project", "subject_id": project_id,
                "scope_type": "project", "scope_id": project_id, "predicate": "unsupported_claim",
                "value_text": "不应写入的推断", "agent_key": "codex-e2e",
            })
            assert rejected.status_code == 200, rejected.text
            rejected_change_id = rejected.json()["change_id"]
            reject_result = client.post(f"/api/agent/tasks/{task_id}/changes/{rejected_change_id}/reject", json={
                "reason": "没有明确证据，不能把推断写成事实。",
            })
            assert reject_result.status_code == 200, reject_result.text
            assert next(x for x in reject_result.json()["task"]["changes"] if x["id"] == rejected_change_id)["status"] == "ignored"

            package = client.get(f"/api/agent/tasks/{task_id}/package")
            assert package.status_code == 200, package.text
            assert len(package.json()["draft_assets"]) == 1
            change_ids = [item["id"] for item in package.json()["task"]["changes"] if item["status"] == "pending"]
            assert len(change_ids) == 2
            assert all("review" in item for item in package.json()["task"]["changes"])

            applied = client.post(f"/api/agent/tasks/{task_id}/apply", json={"change_ids": change_ids})
            assert applied.status_code == 200, applied.text
            assert len(applied.json()["results"]) == 2
            assert len(db.list_career_facts(subject_type="project", subject_id=project_id)) == 1
            assert len(db.list_feedback_notes(scope="project", scope_id=project_id)) == 1

            # A named job-prep document must not become a generic draft asset or
            # appear in the workspace until the user confirms its proposal.
            knowledge_run = client.post("/api/agent/external-runs", json={
                "title": "沉淀岗位面试准备", "instruction": "为当前岗位整理面试准备文档",
                "agent_key": "codex-e2e", "track_id": track_id,
            })
            assert knowledge_run.status_code == 200, knowledge_run.text
            knowledge_task_id = knowledge_run.json()["task"]["id"]
            proposal = client.post("/api/agent/proposals/track-knowledge", json={
                "task_id": knowledge_task_id, "track_id": track_id,
                "title": "项目经历 90 秒表达", "body": "# 项目经历 90 秒表达\n\n用真实动作说明取舍和结果。",
                "folder_key": "interview", "topic": "项目表达", "mastery": "learning",
                "reason": "为本岗位沉淀可反复练习的面试表达", "agent_key": "codex-e2e",
            })
            assert proposal.status_code == 200, proposal.text
            assert not [x for x in db.list_knowledge_items(track_id=track_id)
                        if x.get("title") == "项目经历 90 秒表达"]
            pending_doc = client.get(f"/api/agent/tasks/{knowledge_task_id}/package")
            assert pending_doc.status_code == 200, pending_doc.text
            candidate = pending_doc.json()["task"]["changes"][0]
            assert candidate["action_type"] == "create_track_knowledge"
            assert candidate["review"]["destination"].endswith("/ 面试准备")
            assert client.get(f"/api/agent/tasks/{knowledge_task_id}/package").json()["draft_assets"] == []
            confirmed_doc = client.post(f"/api/agent/tasks/{knowledge_task_id}/apply", json={"change_ids": [candidate["id"]]})
            assert confirmed_doc.status_code == 200, confirmed_doc.text
            created_doc = next(x for x in db.list_knowledge_items(track_id=track_id)
                               if x.get("title") == "项目经历 90 秒表达")
            folder = db.get_knowledge_folder(created_doc["folder_id"])
            assert created_doc["scope_type"] == "track"
            assert created_doc["source_type"] == "external_agent"
            assert folder["name"] == "面试准备"
            completed_doc_task = client.get(f"/api/agent/tasks/{knowledge_task_id}")
            assert completed_doc_task.status_code == 200, completed_doc_task.text
            assert any(x["type"] == "knowledge_item" and x["id"] == created_doc["id"]
                       for x in completed_doc_task.json()["artifacts"])

            # The persistence layer must reject a logical folder key. Otherwise
            # SQLite accepts it in an INTEGER field and the document disappears
            # from every real folder view.
            try:
                db.create_knowledge_item({
                    "title": "不应成为孤儿的文档", "content": "", "scope_type": "track",
                    "track_id": track_id, "folder_id": "professional",
                })
                raise AssertionError("logical folder key should be rejected")
            except ValueError as exc:
                assert "数字 ID" in str(exc)

            # An external Agent receives only the chosen records, but it can see their
            # revisions and the hard feedback rule that the user just confirmed.
            context_package = client.post("/api/agent/context-package", json={
                "track_id": track_id, "project_id": project_id,
                "intent": "prepare_interview_answer",
            })
            assert context_package.status_code == 200, context_package.text
            package_data = context_package.json()
            project_object = next(x for x in package_data["objects"] if x["type"] == "project")
            assert project_object["revision"]
            assert project_object["content"]["document"].startswith("背景")
            assert len(project_object["content"]["document"]) < 1700
            assert "详细面试问答素材" not in package_data["context_text"]
            assert any("不要把团队测试结论" in x["directive"] for x in package_data["constraints"]["hard"])

            full_package = client.post("/api/agent/context-package", json={
                "project_id": project_id, "document_type": "project", "document_id": project_id,
                "intent": "verify_source", "detail": "full",
            })
            assert full_package.status_code == 200, full_package.text
            full_project = next(x for x in full_package.json()["objects"] if x["type"] == "project")
            assert len(full_project["content"]["document"]) > 10000
            assert full_project["revision"] == project_object["revision"]

            current = client.get(f"/api/projects/{project_id}").json()
            stale_base_hash = hashlib.sha256((current.get("document") or "").encode("utf-8")).hexdigest()
            stale_run = client.post("/api/agent/external-runs", json={
                "title": "旧版本项目文档改写", "instruction": "根据项目资料改写项目文档",
                "agent_key": "codex-e2e", "track_id": track_id,
                "object_type": "project", "object_id": project_id,
            })
            assert stale_run.status_code == 200, stale_run.text
            stale_task_id = stale_run.json()["task"]["id"]
            stale_change_id = db.create_proposed_change({
                "task_id": stale_task_id, "run_id": stale_run.json()["run_id"],
                "action_type": "update_project_document", "target_type": "project", "target_id": project_id,
                "proposed_title": "旧版本候选", "proposed_content": "这段旧候选不应覆盖用户的新修改。",
                "reason": "用于验证版本冲突保护", "scope_type": "project",
                "metadata_json": json.dumps({"base_hash": stale_base_hash}, ensure_ascii=False),
            })
            current["document"] += "\n结果：后续使用统一口径继续验证。"
            edited = client.put(f"/api/projects/{project_id}", json={
                key: current.get(key)
                for key in ("name", "one_liner", "document", "technologies", "keywords")
            })
            assert edited.status_code == 200, edited.text
            stale_review = client.get(f"/api/agent/tasks/{stale_task_id}/package")
            assert stale_review.status_code == 200, stale_review.text
            assert stale_review.json()["task"]["changes"][0]["review"]["conflict"] is True
            stale_apply = client.post(f"/api/agent/tasks/{stale_task_id}/apply", json={"change_ids": [stale_change_id]})
            assert stale_apply.status_code == 409, stale_apply.text

            refreshed_package = client.post("/api/agent/context-package", json={
                "track_id": track_id, "project_id": project_id,
                "intent": "prepare_interview_answer",
            })
            assert refreshed_package.status_code == 200, refreshed_package.text
            refreshed_project = next(x for x in refreshed_package.json()["objects"] if x["type"] == "project")
            assert refreshed_project["revision"] != project_object["revision"]
            changes = client.get(f"/api/agent/changes?after={cursor}")
            assert changes.status_code == 200, changes.text
            types = {item["event_type"] for item in changes.json()["items"]}
            assert "project_updated" in types, types
            assert "fact_confirmed" in types, types
            assert "feedback_confirmed" in types, types
            assert "agent_candidate_rejected" in types, types
            print("EXTERNAL_AGENT_E2E_OK", task_id, changes.json()["next_cursor"])


if __name__ == "__main__":
    main()
