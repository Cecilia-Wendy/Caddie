"""Entry identity, message isolation, and knowledge write receipt regressions."""
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


def main():
    with TemporaryDirectory(prefix="caddie-workspace-task-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "data" / "caddie.db"
        vcs.CADDIE_DIR = root / "data"
        vcs.VAULT = vcs.CADDIE_DIR / "vault"
        server._commit = lambda _message: None

        with TestClient(server.app) as client:
            cathay_id = db.create_job_track({"company": "国泰", "role": "股权业务"})
            baidu_id = db.create_job_track({"company": "百度", "role": "AI产品经理"})

            career = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"career_lead:track:{cathay_id}",
                "mode": "career_lead", "title": "国泰 · Agent 工作台",
                "track_id": cathay_id,
            }).json()
            assert client.post(
                f"/api/workspace-sessions/{career['session']['id']}/messages",
                json={"role": "user", "content": "复盘国泰股权一面"},
            ).status_code == 200

            baidu_career = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"career_lead:track:{baidu_id}",
                "mode": "career_lead", "title": "百度 · Agent 工作台",
                "track_id": baidu_id,
            }).json()
            baidu_knowledge = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"knowledge_coach:track:{baidu_id}:new",
                "mode": "knowledge_coach", "title": "百度 · AI产品经理 · 新建知识准备",
                "track_id": baidu_id,
            }).json()
            assert not baidu_career["messages"]
            assert not baidu_knowledge["messages"]
            assert len({
                career["session"]["id"], baidu_career["session"]["id"],
                baidu_knowledge["session"]["id"],
            }) == 3

            restored = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"knowledge_coach:track:{baidu_id}:new",
                "mode": "knowledge_coach", "title": "百度 · 知识教练",
                "track_id": baidu_id,
            }).json()
            assert restored["session"]["id"] == baidu_knowledge["session"]["id"]

            renamed = client.post(
                f"/api/workspace-sessions/{baidu_knowledge['session']['id']}/knowledge-topic",
                json={"topic": "API 与 Skill 基础"},
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["title"] == "API 与 Skill 基础"

            folders, _created = db.ensure_track_knowledge_folders(baidu_id)
            folder = folders[0]
            task_id = db.create_agent_task({
                "task_type": "knowledge_create", "title": "API 基础",
                "instruction": "生成一篇 API 基础文档", "object_type": "job_track",
                "object_id": baidu_id, "track_id": baidu_id,
                "assigned_expert": "knowledge_coach", "status": "review",
                "conversation_id": baidu_knowledge["session"]["id"],
                "context_json": json.dumps({"mode": "knowledge_coach"}, ensure_ascii=False),
            })
            change_id = db.create_proposed_change({
                "task_id": task_id, "action_type": "create_track_knowledge",
                "target_type": "knowledge_item", "parent_type": "job_track",
                "parent_id": baidu_id, "proposed_title": "API基础",
                "proposed_content": "# API基础\n\n候选正文", "scope_type": "track",
                "metadata_json": json.dumps({
                    "track_id": baidu_id, "folder_id": folder["id"],
                    "folder_name": folder["name"], "topic": "API基础",
                }, ensure_ascii=False),
            })
            assert not db.list_knowledge_items(scope_type="track", track_id=baidu_id)

            resumed = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"knowledge_coach:track:{baidu_id}:new",
                "mode": "knowledge_coach", "title": "百度 · 知识教练",
                "track_id": baidu_id,
            }).json()
            assert resumed["agent_task"]["id"] == task_id
            assert resumed["agent_task"]["changes"][0]["review"]["destination"]

            applied = client.post(
                f"/api/agent/tasks/{task_id}/apply",
                json={"change_ids": [change_id]},
            )
            assert applied.status_code == 200, applied.text
            receipt = applied.json()["results"][0]
            assert receipt["logical_path"].startswith("百度 / AI产品经理 / 准备知识 /")
            assert receipt["open_target"] == {
                "view": "track", "track_id": baidu_id, "tab": "knowledge",
                "knowledge_item_id": receipt["knowledge_item_id"],
            }

            document = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": (
                    f"knowledge_coach:track:{baidu_id}:"
                    f"document:{receipt['knowledge_item_id']}"
                ),
                "mode": "knowledge_coach", "title": "API基础 · 知识教练",
                "track_id": baidu_id,
                "knowledge_item_id": receipt["knowledge_item_id"],
            }).json()
            assert document["session"]["id"] == baidu_knowledge["session"]["id"]
            assert document["session"]["title"] == "API基础"

            fresh = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"knowledge_coach:track:{baidu_id}:new",
                "mode": "knowledge_coach", "title": "百度 · AI产品经理 · 新建知识准备",
                "track_id": baidu_id,
            }).json()
            assert fresh["session"]["id"] != baidu_knowledge["session"]["id"]
            assert fresh["messages"] == []

            cathay_knowledge = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"knowledge_coach:track:{cathay_id}:new",
                "mode": "knowledge_coach", "title": "国泰 · 股权业务 · 新建知识准备",
                "track_id": cathay_id,
            }).json()
            sessions = client.get("/api/sessions").json()
            by_id = {item["id"]: item for item in sessions}
            assert by_id[fresh["session"]["id"]]["track_company"] == "百度"
            assert by_id[cathay_knowledge["session"]["id"]]["track_company"] == "国泰"
            assert by_id[document["session"]["id"]]["latest_expert_key"] == "knowledge_coach"
            assert by_id[document["session"]["id"]]["title"] != "知识教练"

            # 两个岗位中即使文档同名，文档任务身份仍然分别绑定岗位与文档。
            cathay_doc_id = db.create_knowledge_item({
                "title": "API基础", "content": "# API基础", "scope_type": "track",
                "track_id": cathay_id, "topic": "API基础", "status": "active",
            })
            cathay_doc = client.post("/api/workspace-sessions/resolve", json={
                "workspace_task_key": f"knowledge_coach:track:{cathay_id}:document:{cathay_doc_id}",
                "mode": "knowledge_coach", "title": "API基础", "track_id": cathay_id,
                "knowledge_item_id": cathay_doc_id,
            }).json()
            assert cathay_doc["session"]["id"] != document["session"]["id"]

    print("WORKSPACE_TASK_ISOLATION_OK")


if __name__ == "__main__":
    main()
