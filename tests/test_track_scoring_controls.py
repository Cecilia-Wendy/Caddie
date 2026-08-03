"""Regression coverage for editable job requirements and score exclusions."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import server
import vcs


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

        track_id = db.create_job_track({
            "track_group": "AI 产品",
            "company": "示例公司",
            "role": "Agent 产品实习生",
            "jd": "base：北京\n必须在 8 月 15 日前到岗\n熟悉 Agent 产品与数据分析",
        })
        location_id = db.create_track_gap({
            "track_id": track_id,
            "dimension": "岗位要求",
            "requirement": "base：北京",
        })
        skill_id = db.create_track_gap({
            "track_id": track_id,
            "dimension": "硬技能",
            "requirement": "熟悉 Agent 产品与数据分析",
            "my_status": "have",
        })

        result = server.set_gap_score_exclusion(location_id, {
            "excluded": True,
            "remember": True,
        })
        assert result["excluded_from_score"] is True
        assert result["learned"] is True
        excluded = db.get_track_gap(location_id)
        assert excluded["excluded_from_score"] == 1
        assert "地点" in excluded["exclusion_reason"]
        assert db.get_track_gap(skill_id)["excluded_from_score"] == 0

        notes = db.list_feedback_notes(scope="global", status="active")
        assert any(x.get("category") == "match_scoring" for x in notes)
        assert server._non_scoring_requirement_reason("必须在 8 月 15 日前到岗")
        assert server._non_scoring_requirement_reason("mentor 愿意听实习生的想法")
        assert server._non_scoring_requirement_reason("负责 Agent 产品策略") is None

        server.set_gap_score_exclusion(location_id, {
            "excluded": False,
            "remember": False,
        })
        assert db.get_track_gap(location_id)["excluded_from_score"] == 0

    db.DB_PATH = original_db_path
    vcs.VAULT = original_vault
    server._commit = original_commit
    print("TRACK_SCORING_CONTROLS_OK")


if __name__ == "__main__":
    main()
