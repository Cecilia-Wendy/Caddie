"""Isolated smoke test for executable expert workflows."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_runtime
import db
import server


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "caddie.db"
        db.init_db()
        track_id = db.create_job_track({
            "company": "测试公司", "role": "基金产品", "jd": "负责基金产品研究和分析",
        })
        resume_id = db.create_resume_version({
            "track_id": track_id, "version_name": "测试简历", "extracted_text": "参与基金产品分析。",
        })
        server._expert_model_selection = lambda expert: {
            "profile_key": expert.get("model_profile") or "test", "profile_name": "测试",
            "provider_id": "test", "provider_name": "测试模型", "model": "test-model",
            "provider": {}, "uses_fallback": False,
        }
        server.caddie_context.build_context = lambda **kwargs: {
            "text": "JD：负责基金产品研究。经历：参与基金产品分析。", "refs": [],
        }
        server.ai.chat = lambda *args, **kwargs: (
            "SUMMARY: 已形成可执行方案\nTITLE: 测试专家产出\n"
            "===DOCUMENT===\n# 测试专家产出\n\n- 基于真实资料形成。"
        )
        server._commit = lambda message: None

        cases = [
            ("resume_editor", "resume_version", resume_id, "诊断简历"),
            ("pressure_interviewer", "job_track", track_id, "设计模拟面试"),
            ("review_analyst", "job_track", track_id, "复盘逐字稿"),
        ]
        for expert, object_type, object_id, instruction in cases:
            route = agent_runtime.coordinate(
                instruction, object_type=object_type, object_id=object_id, track_id=track_id,
            )
            task_id = db.create_agent_task({
                "task_type": route["task_type"], "title": instruction, "instruction": instruction,
                "object_type": object_type, "object_id": object_id, "track_id": track_id,
                "assigned_expert": expert,
            })
            server._run_agent_task(task_id)
            task = db.get_agent_task(task_id)
            assert task["status"] == "review", task
            pending = [item for item in task["changes"] if item["status"] == "pending"]
            assert len(pending) == 1 and pending[0]["action_type"] == "create_asset", pending
            server.apply_agent_task(task_id, server.AgentTaskApplyIn(change_ids=[pending[0]["id"]]))
            assert db.get_agent_task(task_id)["status"] == "completed"

        for expert in ("career_lead", "job_researcher"):
            task_id = db.create_agent_task({
                "task_type": agent_runtime.TASK_TYPES[expert], "title": "分析岗位",
                "instruction": "分析岗位并安排下一步", "object_type": "job_track",
                "object_id": track_id, "track_id": track_id, "assigned_expert": expert,
            })
            server._run_agent_task(task_id)
            task = db.get_agent_task(task_id)
            assert task["status"] == "completed" and task["result_summary"], task
            assert not task["changes"], task["changes"]

        assets = db.list_assets(track_id=track_id)
        assert {item["asset_type"] for item in assets} == {
            "resume_edit_plan", "mock_interview_plan", "review",
        }
        print(f"EXPERT_RUNTIME_OK tasks={len(cases) + 2} assets={len(assets)}")


if __name__ == "__main__":
    main()
