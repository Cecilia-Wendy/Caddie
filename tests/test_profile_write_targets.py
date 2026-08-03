"""Confirmed profile writes return durable, navigable target identities."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db


def main():
    original_db_path = db.DB_PATH
    with TemporaryDirectory(prefix="caddie-profile-write-") as temp:
        db.DB_PATH = Path(temp) / "caddie.db"
        db.init_db()
        results, application_ids, targets = db.apply_changes([{
            "type": "create_experience",
            "company": "个人研究",
            "role": "研究项目",
            "start_date": "2026-01",
            "end_date": "2026-01",
            "projects": [
                {"name": "项目一", "document": "# 项目一\n\n正文"},
                {"name": "项目二", "document": "# 项目二\n\n正文"},
            ],
        }])
        assert application_ids == []
        assert results == [
            "新增经历：个人研究 · 研究项目",
            "  └ 新增项目：项目一",
            "  └ 新增项目：项目二",
        ]
        assert [item["type"] for item in targets] == [
            "experience", "project", "project",
        ]
        experience_id = targets[0]["id"]
        assert all(item.get("parent_id") == experience_id for item in targets[1:])
        experience = db.get_experience(experience_id)
        assert experience["company"] == "个人研究"
        projects = next(
            item["projects"] for item in db.get_experiences()
            if item["id"] == experience_id
        )
        assert [item["name"] for item in projects] == ["项目一", "项目二"]

    db.DB_PATH = original_db_path
    print("PROFILE_WRITE_TARGETS_OK")


if __name__ == "__main__":
    main()
