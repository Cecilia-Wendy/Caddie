import tempfile
from pathlib import Path

import db


def test_company_submission_material_versions_and_usage():
    original_path, original_journal = db.DB_PATH, db._JOURNAL_CONFIGURED
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db.DB_PATH = Path(tmp) / "company-submission.db"
            db._JOURNAL_CONFIGURED = False
            db.init_db()
            track_id = db.create_job_track({"company": "示例公司", "role": "产品经理"})
            material_id = db.create_company_submission_material({
                "company": "示例公司", "material_type": "question_bank",
                "title": "网申常见问答", "content": "母版回答",
            })
            db.update_company_submission_material(material_id, {
                "content": "岗位定制后的回答", "change_summary": "针对产品岗修改",
            })
            usage = db.create_company_submission_usage(material_id, {
                "track_id": track_id, "notes": "已上传官网",
            })
            items = db.list_company_submission_materials("示例公司")
            assert len(items) == 1
            assert items[0]["current_version"] == 2
            assert [x["version"] for x in items[0]["versions"]] == [2, 1]
            assert usage["material_version"] == 2
            assert items[0]["usages"][0]["role"] == "产品经理"

            capture_id = db.create_company_application_capture({
                "company": "示例公司",
                "track_id": track_id,
                "title": "示例公司校园招聘申请",
                "page_url": "https://jobs.example.com/apply/step-2",
                "portal_host": "jobs.example.com",
                "structure": [
                    {
                        "title": "教育经历",
                        "fields": [
                            {
                                "label": "学校名称",
                                "semantic_key": "education",
                                "control_type": "text",
                                "value": "示例大学",
                                "required": True,
                            },
                            {
                                "label": "毕业时间",
                                "semantic_key": "availability_date",
                                "control_type": "date",
                                "value": "2026-06",
                                "required": False,
                            },
                        ],
                    },
                    {
                        "title": "开放问题",
                        "fields": [
                            {
                                "label": "为什么申请我们",
                                "semantic_key": "company_motivation",
                                "control_type": "textarea",
                                "value": "示例回答",
                                "required": True,
                            },
                        ],
                    },
                ],
            })
            captures = db.list_company_application_captures("示例公司")
            assert capture_id > 0
            assert len(captures) == 1
            assert len(db.list_all_company_application_captures()) == 1
            assert db.list_company_submission_material_companies() == ["示例公司"]
            assert captures[0]["role"] == "产品经理"
            assert captures[0]["field_count"] == 3
            assert captures[0]["structure"][1]["fields"][0]["semantic_key"] == "company_motivation"

            updated = db.update_company_application_capture(capture_id, {
                "title": "整理后的网申",
                "track_id": track_id,
                "structure": [{
                    "title": "教育经历",
                    "archived": True,
                    "fields": [{"label": "学校", "value": "示例大学", "semantic_key": "education"}],
                }],
            })
            assert updated["title"] == "整理后的网申"
            assert updated["field_count"] == 1
            assert updated["structure"][0]["archived"] is True
            catalog = db.company_application_module_catalog("示例公司")
            assert catalog["company_modules"][0]["title"] == "教育经历"
            assert catalog["company_modules"][0]["scope"] == "capture_unique"
            assert db.delete_company_application_capture(capture_id)
            assert db.list_company_application_captures("示例公司") == []

            assert db.archive_company_submission_material(material_id)
            assert db.list_company_submission_materials("示例公司") == []
    finally:
        db.DB_PATH, db._JOURNAL_CONFIGURED = original_path, original_journal
