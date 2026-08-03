"""Contract test for complete interview-round context in Agent workflows."""
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import db
import interview_store
import server


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "caddie.db"
        server._commit = lambda _message: None
        captured = {}

        with TestClient(server.app) as client:
            track_id = db.create_job_track({
                "company": "测试公司", "role": "产品经理", "jd": "负责产品规划与跨团队推进",
            })
            round_id = interview_store.create_round(track_id, {
                "round_name": "产品二面", "round_type": "professional",
            })
            interview_store.ensure_round_documents(round_id)
            interview_store.create_transcript_source(round_id, {
                "source_kind": "paste", "title": "二面原始逐字稿",
                "raw_text": "面试官：为什么做这个产品？\n王玺：因为用户资料分散，反馈无法复用。",
            })

            response = client.post("/api/agent/context-package", json={
                "interview_round_id": round_id, "intent": "interview_review", "detail": "full",
            })
            assert response.status_code == 200, response.text
            package = response.json()
            assert package["scope"]["track_id"] == track_id
            round_object = next(x for x in package["objects"] if x["type"] == "interview_round")
            assert "为什么做这个产品" in round_object["content"]["transcript_sources"][0]["raw_text"]

            server._expert_model_selection = lambda expert: {
                "profile_key": "test", "profile_name": "测试", "provider_id": "test",
                "provider_name": "测试", "model": "test", "provider": {},
                "uses_fallback": False,
            }

            def fake_chat(messages, **_kwargs):
                captured["material"] = messages[0]["content"]
                return (
                    "SUMMARY: 已基于逐字稿完成复盘\nTITLE: 产品二面复盘\n"
                    "===DOCUMENT===\n# 产品二面复盘\n\n"
                    "## 问答还原\n\n面试官询问为什么做这个产品。\n\n"
                    "## 下一轮行动\n\n补充用户证据。"
                )

            server.ai.chat = fake_chat
            task_id = db.create_agent_task({
                "task_type": "interview_review", "title": "复盘产品二面",
                "instruction": "基于逐字稿复盘", "object_type": "interview_round",
                "object_id": round_id, "track_id": track_id, "assigned_expert": "review_analyst",
            })
            server._run_agent_task(task_id)
            assert "为什么做这个产品" in captured["material"]
            assert db.get_agent_task(task_id)["status"] == "review"

        print("AGENT_INTERVIEW_CONTEXT_OK", round_id)


if __name__ == "__main__":
    main()
