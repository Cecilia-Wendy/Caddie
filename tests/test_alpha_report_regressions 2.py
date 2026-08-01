"""Regression coverage for defects reported by the GLM/Qwen Alpha review."""
from pathlib import Path
import sys
import tempfile

from fastapi import HTTPException
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import interview_store
import server
import vcs


def expect_validation(factory, **payload):
    try:
        factory(**payload)
    except ValidationError:
        return
    raise AssertionError(f"{factory.__name__} unexpectedly accepted {payload}")


def main():
    original_db_path = db.DB_PATH
    original_vault = vcs.VAULT
    original_commit = server._commit
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db.DB_PATH = root / "caddie.db"
        vcs.VAULT = root / "vault"
        server._commit = lambda _message: True
        db.init_db()

        conn = db.get_db()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()

        expect_validation(
            server.ApplicationIn,
            company="示例公司",
            role="产品经理",
            applied_date="invalid-date",
        )
        expect_validation(
            server.ApplicationIn,
            company="示例公司",
            role="产品经理",
            status="not-a-status",
        )
        expect_validation(
            server.ApplicationIn,
            company="X" * 201,
            role="产品经理",
        )
        expect_validation(
            server.DocumentV2In,
            title="错误字段文档",
            document_type="note",
            content="该字段必须被拒绝，不能静默忽略",
        )
        expect_validation(
            server.SourceIn,
            source_type="job_description",
            content="拼错的资料类型必须被拒绝",
        )

        experience = server.add_experience(server.ExperienceIn(
            company="滴滴自动驾驶",
            role="产品经理",
            start_date="2025-01-01",
        ))
        eid = experience["id"]
        project = server.add_project(eid, server.ProjectIn(
            name="窄距通行项目",
            one_liner="优化窄距场景体验",
            document="负责场景定义、数据分析和落地验证。",
        ))
        pid = project["id"]

        assert server.get_experience(eid)["company"] == "滴滴自动驾驶"
        assert server.get_experience_projects(eid)["items"][0]["id"] == pid
        edited = server.edit_experience(eid, server.ExperienceIn(
            company="滴滴",
            role="高级产品经理",
            start_date="2025-01-01",
        ))
        assert edited["item"]["role"] == "高级产品经理"

        search_titles = [item["title"] for item in db.search("滴滴")]
        assert "窄距通行项目" in search_titles, search_titles
        assert db.search("窄距"), "two-character CJK substring must be searchable"

        db.upsert_semantic_chunk({
            "chunk_key": f"project:{pid}:0",
            "entity_type": "project",
            "entity_id": pid,
            "title": "窄距通行项目",
            "content": "窄距场景",
            "content_hash": "test",
            "vector_json": "[1.0]",
            "metadata_json": "{}",
        })
        assert db.search_index_health()["semantic_chunks"] == 1
        server.del_project(pid)
        health = db.search_index_health()
        assert health["semantic_chunks"] == 0
        assert health["orphan_projects"] == 0
        try:
            server.del_project(pid)
            raise AssertionError("deleting a missing project must return 404")
        except HTTPException as exc:
            assert exc.status_code == 404

        application = server.add_application(server.ApplicationIn(
            company="字节跳动",
            role="AI 产品经理",
            applied_date="2026-07-26",
        ))
        aid = application["id"]
        assert server.get_application(aid)["company"] == "字节跳动"
        tid = db.create_job_track({"company": "字节跳动", "role": "AI 产品经理"})
        assert server.link_application_track(
            aid, server.ApplicationTrackIn(track_id=tid)
        )["ok"]
        try:
            server.link_application_track(
                aid, server.ApplicationTrackIn(track_id=999_999)
            )
            raise AssertionError("linking a missing track must return 422")
        except HTTPException as exc:
            assert exc.status_code == 422
        try:
            server.move_application(aid, server.ApplicationStatusIn(status="offer"))
            raise AssertionError("illegal status transition must return 409")
        except HTTPException as exc:
            assert exc.status_code == 409
        server.move_application(aid, server.ApplicationStatusIn(status="screening"))
        assert server.get_application(aid)["status"] == "screening"
        server.move_application(aid, server.ApplicationStatusIn(status="active"))
        assert server.get_application(aid)["status"] == "active"
        server.del_application(aid)
        try:
            server.del_application(aid)
            raise AssertionError("deleting a missing application must return 404")
        except HTTPException as exc:
            assert exc.status_code == 404

        sid = server.new_session()["id"]
        db.save_message(sid, "user", "第一轮")
        assert server.chat_history_legacy(sid)[0]["content"] == "第一轮"

        document = server.create_document_v2(server.DocumentV2In(
            title="面试准备",
            document_type="round_preparation",
            body="正文",
        ))
        did = document["data"]["id"]
        assert server.archive_document_v2(did)["status"] == "archived"
        assert interview_store.get_document(did) is None
        assert server.restore_document_v2(did)["data"]["status"] == "active"

        db.replace_search_document("project", 999_999, "幽灵项目", "不应保留")
        db.delete_stale_search_documents({("job_track", tid)})
        assert not db.search("幽灵项目")

        enums = server.api_enum_contract()
        assert "applied" in enums["application_status"]
        assert "review_report" in enums["document_types"]

        paths = {route.path for route in server.app.routes}
        required = {
            "/api/version",
            "/api/meta/enums",
            "/api/applications/{aid}",
            "/api/experiences/{eid}",
            "/api/experiences/{eid}/projects",
            "/api/projects",
            "/api/chat/history",
            "/api/tracks",
            "/api/job-lines",
            "/api/settings/providers",
            "/api/worklog",
            "/api/retrieval/index/health",
            "/api/v2/documents/{did}",
            "/api/v2/documents/{did}/restore",
        }
        assert not (required - paths), sorted(required - paths)

    db.DB_PATH = original_db_path
    vcs.VAULT = original_vault
    server._commit = original_commit
    print("ALPHA_REPORT_REGRESSIONS_OK")


if __name__ == "__main__":
    main()
