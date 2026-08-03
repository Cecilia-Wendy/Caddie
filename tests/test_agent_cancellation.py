"""Regression test: cancelling during a model call must prevent later writes."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai
import db
import server
import vcs


def main():
    original_chat = ai.chat
    original_selection = server._expert_model_selection
    original_db_path = db.DB_PATH
    original_caddie_dir = vcs.CADDIE_DIR
    original_vault = vcs.VAULT
    with TemporaryDirectory(prefix="caddie-agent-cancel-") as temp:
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
        db.init_db()
        task_id = db.create_agent_task({
            "task_type": "knowledge",
            "title": "取消中的知识任务",
            "instruction": "整理一份知识文档并保存",
            "assigned_expert": "knowledge_coach",
            "status": "queued",
        })

        def cancel_during_model(*_args, **_kwargs):
            server.cancel_agent_task(task_id)
            return (
                "SUMMARY: 不应写入\nTITLE: 取消后的内容\n===DOCUMENT===\n"
                "# 取消后的内容\n\n这段内容不得形成候选。"
            )

        ai.chat = cancel_during_model
        server._run_agent_task(task_id)
        task = db.get_agent_task(task_id)
        assert task["status"] == "cancelled", task
        assert not task.get("changes"), task.get("changes")
        assert not db.list_knowledge_items(scope_type="global")

    ai.chat = original_chat
    server._expert_model_selection = original_selection
    db.DB_PATH = original_db_path
    vcs.CADDIE_DIR = original_caddie_dir
    vcs.VAULT = original_vault
    print("AGENT_CANCELLATION_OK")


if __name__ == "__main__":
    main()
