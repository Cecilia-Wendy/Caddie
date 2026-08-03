from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import db
import server
import vcs


def test_resume_migration_writes_profile_and_experience_after_confirmation():
    with TemporaryDirectory(prefix="caddie-resume-migration-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "caddie.db"
        vcs.CADDIE_DIR = root
        vcs.VAULT = root / "vault"
        server._commit = lambda _message: None
        with TestClient(server.app) as client:
            task = client.post("/api/agent/external-runs", json={
                "title": "拆分简历", "instruction": "把简历写入个人资料和经历",
                "agent_key": "codex-test",
            }).json()["task"]
            profile = client.post("/api/agent/proposals/profile", json={
                "task_id": task["id"], "changes": {"name": "测试用户", "location": "北京"},
                "agent_key": "codex-test",
            })
            experience = client.post("/api/agent/proposals/experience", json={
                "task_id": task["id"], "company": "示例公司", "role": "产品实习生",
                "start_date": "2026-01-01", "end_date": "2026-04-01",
                "description": "负责需求拆解与跨团队落地。", "agent_key": "codex-test",
            })
            assert profile.status_code == 200, profile.text
            assert experience.status_code == 200, experience.text
            package = client.get(f"/api/agent/tasks/{task['id']}/package").json()
            changes = package["task"]["changes"]
            assert {x["action_type"] for x in changes} == {"update_user_profile", "create_experience"}
            applied = client.post(
                f"/api/agent/tasks/{task['id']}/apply",
                json={"change_ids": [x["id"] for x in changes]},
            )
            assert applied.status_code == 200, applied.text
            assert client.get("/api/user-profile").json()["profile"]["name"] == "测试用户"
            stored = client.get("/api/experiences").json()
            assert stored[0]["company"] == "示例公司"
            detail = client.get(f"/api/experiences/{stored[0]['id']}").json()
            assert detail["projects"][0]["document"] == "负责需求拆解与跨团队落地。"


def test_apple_calendar_uses_native_calendar_handoff():
    with TemporaryDirectory(prefix="caddie-calendar-") as temp:
        root = Path(temp)
        db.DB_PATH = root / "caddie.db"
        vcs.CADDIE_DIR = root
        vcs.VAULT = root / "vault"
        server._commit = lambda _message: None
        with TestClient(server.app) as client:
            created = client.post("/api/calendar-items", json={
                "title": "准备面试", "starts_at": "2026-08-05 10:00",
                "duration_minutes": 30, "item_type": "task",
            })
            assert created.status_code == 200, created.text
            with mock.patch("server.sys.platform", "darwin"), mock.patch("server.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                opened = client.post(
                    "/api/calendar/open-apple",
                    params={"item_kind": "career_calendar", "item_id": created.json()["id"]},
                )
            assert opened.status_code == 200, opened.text
            assert opened.json()["event_count"] == 1
            command = run.call_args.args[0]
            assert command[:3] == ["open", "-a", "Calendar"]
            assert Path(command[3]).read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR")


def test_supabase_schema_accepts_current_event_registry():
    sql = (ROOT / "supabase" / "telemetry_schema.sql").read_text(encoding="utf-8")
    for event in ("app_started", "feature_action_completed", "ai_call_completed", "external_agent_tool_completed"):
        assert f"'{event}'" in sql
