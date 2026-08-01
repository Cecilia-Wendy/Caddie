"""Regression coverage for company research folders and documents."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


def main():
    original_db_path = db.DB_PATH
    original_journal = db._JOURNAL_CONFIGURED
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "caddie.db"
        db._JOURNAL_CONFIGURED = False
        db.init_db()

        folders, created = db.ensure_company_knowledge_folders("示例公司")
        assert len(created) == 5
        assert [x["name"] for x in folders] == [
            "公司概况", "部门与组织", "业务与产品", "投递记录", "投递规则",
        ]
        rules = next(x for x in folders if x["name"] == "投递规则")
        item_id = db.create_knowledge_item({
            "title": "校招投递限制",
            "content": "同一招聘周期最多投递三个岗位。",
            "scope_type": "company",
            "company": "示例公司",
            "folder_id": rules["id"],
            "mastery": "learning",
            "status": "active",
        })
        item = db.get_knowledge_item(item_id)
        assert item["folder_id"] == rules["id"]
        assert item["company"] == "示例公司"

        folders_again, created_again = db.ensure_company_knowledge_folders("示例公司")
        assert len(folders_again) == 5
        assert created_again == []

    db.DB_PATH = original_db_path
    db._JOURNAL_CONFIGURED = original_journal
    print("COMPANY_RESEARCH_WORKSPACE_OK")


if __name__ == "__main__":
    main()
