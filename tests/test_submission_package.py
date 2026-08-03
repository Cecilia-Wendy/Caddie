"""Regression coverage for per-application submission packages."""
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

        track_id = db.create_job_track({
            "track_group": "产品",
            "company": "示例公司",
            "role": "产品经理",
        })
        application_id = db.create_application({
            "track_id": track_id,
            "company": "示例公司",
            "role": "产品经理",
            "applied_date": "2026-07-31",
            "status": "applied",
            "source": "邮件投递",
        })
        resume_id = db.create_resume_version({
            "track_id": track_id,
            "version_name": "邮件投递版",
            "docx_path": "/tmp/resume.docx",
            "status": "submitted",
        })
        assert db.set_application_resume_version(application_id, resume_id)
        assert db.get_application(application_id)["resume_version_id"] == resume_id

        material_id = db.create_submission_material({
            "track_id": track_id,
            "application_id": application_id,
            "material_type": "email",
            "title": "投递邮件",
            "content": "您好，附件为我的简历。",
            "external_ref": "邮件主题：产品经理申请",
        })
        items = db.list_submission_materials(track_id)
        assert len(items) == 1
        assert items[0]["application_id"] == application_id
        assert items[0]["frozen"] == 1

        assert db.update_submission_material(material_id, {
            "title": "正式投递邮件",
            "content": "更新后的邮件正文",
        })
        assert db.get_submission_material(material_id)["title"] == "正式投递邮件"
        assert db.delete_submission_material(material_id)
        assert db.list_submission_materials(track_id) == []

    db.DB_PATH = original_db_path
    db._JOURNAL_CONFIGURED = original_journal
    print("SUBMISSION_PACKAGE_OK")


if __name__ == "__main__":
    main()
