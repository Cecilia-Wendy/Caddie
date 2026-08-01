"""Regression coverage for archive clarifications and status corrections."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_runtime
import db
import server


def main():
    original_db_path = db.DB_PATH
    original_journal = db._JOURNAL_CONFIGURED
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "caddie.db"
        db._JOURNAL_CONFIGURED = False
        db.init_db()
        application_id = db.create_application({
            "company": "国泰海通",
            "role": "股权业务实习生",
            "status": "rejected",
            "source": "公众号",
        })
        history = [
            {"role": "user", "content": "帮我新建一条求职线，是国泰海通的投行部，base北京"},
            {"role": "user", "content": "我觉得这个岗位就是那个股权岗位，把二者合并并更新进度"},
            {"role": "user", "content": """针对写入前的确认，我的选择如下：

1. 之前的 rejected 状态是否准确？
答：是同一个岗位，之前的rejected状态是错的，请更新为最新进展

2. 需要确认具体岗位名称后才能写入。
答：投行股权实习生

3. 关于当前进展
答：仍在面试流程中

4. 原始投递来源是什么？
答：求职公众号，邮件投递"""},
        ]
        proposal = server._prepare_proposed_changes({
            "changes": [{
                "type": "create_application",
                "company": "国泰海通",
                "role": "股权业务实习生",
                "status": "screening",
            }],
            "clarifications": [
                "需要确认具体岗位名称后才能写入。",
                "关于当前进展是什么？",
            ],
        }, history)

        change = proposal["changes"][0]
        assert change["type"] == "update_application"
        assert change["application_id"] == application_id
        assert change["role"] == "投行股权实习生"
        assert change["source"] == "求职公众号，邮件投递"
        assert change["allow_status_correction"] is True
        assert proposal["clarifications"] == []

        # A proposal generated before this fix can still be retried: the apply
        # endpoint signs it from persisted user correction evidence.
        session_id = "status-correction-session"
        db.create_session(session_id)
        for message in history:
            db.save_message(session_id, message["role"], message["content"])
        old_change = dict(change)
        old_change.pop("allow_status_correction", None)
        authorized = server._authorize_profile_status_corrections(
            [old_change], session_id
        )
        assert authorized[0]["allow_status_correction"] is True

        results, _, _ = db.apply_changes(proposal["changes"])
        assert not any("失败" in item for item in results), results
        assert db.get_application(application_id)["status"] == "screening"

        duplicate_id = db.create_application({
            "company": "国泰海通",
            "role": "投行部日常实习生",
            "status": "screening",
            "source": "待确认",
            "notes": "7月28日 HR 电话联系，已加微信",
            "remark_tag": "resume_pending",
        })
        db.update_application(application_id, {
            **db.get_application(application_id),
            "status": "rejected",
            "remark_tag": "first",
        })
        kept_id = db.merge_applications(duplicate_id, application_id, {
            "role": "投行股权实习生",
            "status": "screening",
            "source": "求职公众号，邮件投递",
            "remark_tag": "follow",
        })
        assert kept_id == application_id
        assert db.get_application(duplicate_id) is None
        merged = db.get_application(application_id)
        assert merged["status"] == "screening"
        assert merged["remark_tag"] == "follow"
        assert "7月28日" in merged["notes"]

        # The ordinary state machine remains strict without explicit correction.
        db.set_application_status(application_id, "rejected")
        try:
            db.update_application(application_id, {
                **db.get_application(application_id), "status": "screening",
            })
            raise AssertionError("ordinary rejected -> screening must remain blocked")
        except ValueError:
            pass

        assert server._proposal_task_window(history)[0]["content"].startswith("帮我新建")
        assert agent_runtime.coordinate(
            "针对写入前的确认，我的选择如下"
        )["expert_key"] == "career_lead"

    db.DB_PATH = original_db_path
    db._JOURNAL_CONFIGURED = original_journal
    print("PROFILE_WRITE_CORRECTIONS_OK")


if __name__ == "__main__":
    main()
