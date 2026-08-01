"""Regression check for interview questions becoming reusable knowledge assets."""
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import interview_store
import server


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "caddie.db"
        server._commit = lambda _message: None
        db.init_db()

        experience_id = db.create_experience({
            "company": "对外经济贸易大学",
            "role": "金融硕士",
            "start_date": "2025-09",
            "end_date": "2027-06",
            "location": "北京",
        })
        project_id = db.create_project(experience_id, {
            "name": "注册制下 IPO 审核问询与定价风险研究",
            "one_liner": "毕业论文研究项目",
        })
        track_id = db.create_job_track({
            "company": "测试券商", "role": "股权业务实习生", "jd": "参与 IPO 项目执行",
        })
        first_round = interview_store.create_round(track_id, {
            "round_name": "业务一面", "round_type": "业务面",
        })
        interview_store.ensure_round_documents(first_round)
        interview_store.replace_exam(first_round, [{
            "original_question": "为什么选择这个毕业论文题目？",
            "normalized_question": "为什么选择 IPO 审核问询与定价风险作为论文选题？",
            "intent": "验证选题动机与投行业务关联",
            "answers": [],
        }])
        questions = interview_store.get_exam(first_round)
        server._suggest_interview_question_classifications(questions)
        suggested = interview_store.get_question(questions[0]["id"])
        assert suggested["confirmed"] == 0
        assert suggested["entity_links"][0]["entity_id"] == project_id
        assert suggested["entity_links"][0]["aspect"] == "选题与动机"

        interview_store.save_question_classification(suggested["id"], {
            "question_type": "项目深挖",
            "ability_key": "事实与证据",
            "tags": [{"type": "追问角度", "value": "选题与动机", "confirmed": True}],
            "links": [{
                "entity_type": "project", "entity_id": project_id,
                "entity_title": "注册制下 IPO 审核问询与定价风险研究",
                "relation": "asked_about", "aspect": "选题与动机", "confirmed": True,
            }],
            "confirmed": True,
        })
        linked = interview_store.list_linked_questions("project", project_id)
        assert len(linked) == 1

        saved_answer = server.save_interview_question_answer_v2(
            suggested["id"],
            server.InterviewQuestionAnswerIn(
                body="我选择这个题目，是因为它把审核过程与 IPO 定价风险连接起来。"
            ),
        )
        assert saved_answer["data"]["version"]["version"] == 1
        saved_answer = server.save_interview_question_answer_v2(
            suggested["id"],
            server.InterviewQuestionAnswerIn(
                body="我希望验证审核问询是否包含可用于识别 IPO 定价风险的信息。"
            ),
        )
        assert saved_answer["data"]["version"]["version"] == 2
        question_with_answer = interview_store.get_question(suggested["id"])
        assert question_with_answer["answer_workspace"]["current_version"] == 2
        assert "审核问询" in question_with_answer["answer_workspace"]["current_answer"]
        assert len(question_with_answer["answer_versions"]) == 2

        parsed_coaching = server._parse_interview_question_coaching("""===INTENT===
验证候选人能否把研究问题与投行业务判断连接起来。
===WHY_ASKED===
面试官沿着毕业论文继续追问，是为了验证研究是否真实推进，以及候选人是否理解审核流程。
===ANSWER_SUMMARY===
候选人希望研究审核问询是否包含识别 IPO 定价风险的信息。
===STRENGTHS===
- 研究问题与 IPO 审核直接相关
===ISSUES===
- 证据缺口：没有说明当前样本和阶段性发现
===SATISFACTION_CRITERIA===
- 先给出清晰的研究结论或阶段性判断
- 说明数据、方法与投行业务启示
===ANSWER_STRATEGY===
先用一句话说明研究问题，再说明数据和识别方法，最后交代当前进度、边界及业务启示。
===IMPROVED_ANSWER===
我希望验证审核问询是否包含识别 IPO 定价风险的信息。目前仍在研究阶段，[待补充：阶段性样本与发现]。
===FOLLOWUPS===
- 你的核心变量如何定义？
===EVIDENCE_GAPS===
- 当前样本量和阶段性结果
===CONFIDENCE_NOTE===
研究问题来自现有答案，样本和结果仍需本人确认。""")
        assert server._valid_interview_question_coaching(parsed_coaching, True)
        interview_store.save_question_coaching_version(suggested["id"], {
            **parsed_coaching,
            "source_answer_version": 2,
            "model_provider": "test-provider",
            "model_name": "test-model",
        })
        coached_question = interview_store.get_question(suggested["id"])
        coaching = coached_question["answer_workspace"]["question_coaching"]
        assert coaching["version"] == 1
        assert coaching["source_answer_version"] == 2
        assert coaching["issues"][0].startswith("证据缺口")
        assert len(coaching["satisfaction_criteria"]) == 2
        discussion = server._parse_question_coach_discussion("""===REPLY===
你指出得对，这里更核心的是验证研究是否真实推进，而不是单纯判断表达结构。
===UPDATE_INTENT===
验证研究进展、方法严谨性及其与投行业务的关联。
===UPDATE_WHY_ASKED===
无
===UPDATE_ISSUES===
- 事实缺口：没有给出当前数据准备和阶段性检验进度
===UPDATE_CRITERIA===
无
===UPDATE_STRATEGY===
先给阶段性结论，再交代数据与方法，最后说明业务启示和研究边界。
===UPDATE_ANSWER===
无
===UPDATE_FOLLOWUPS===
- 目前已经完成了哪些数据整理？
===UPDATE_EVIDENCE_GAPS===
- 当前样本和回归进度""")
        assert discussion["reply"].startswith("你指出得对")
        assert "interviewer_intent" in discussion["proposal"]
        assert "why_asked" not in discussion["proposal"]
        message = interview_store.save_question_coaching_message(
            suggested["id"], {
                "role": "assistant",
                "content": discussion["reply"],
                "proposal": discussion["proposal"],
                "coaching_version": 1,
                "answer_version": 2,
                "model_provider": "test-provider",
            },
        )
        assert message["proposal"]["issues"][0].startswith("事实缺口")
        interview_store.mark_question_coaching_message_applied(message["id"])
        coached_question = interview_store.get_question(suggested["id"])
        assert coached_question["answer_workspace"]["coaching_messages"][0]["applied"] == 1

        second_round = interview_store.create_round(track_id, {
            "round_name": "业务二面", "round_type": "业务面",
        })
        interview_store.ensure_round_documents(second_round)
        result = server.use_interview_question_in_preparation_v2(
            second_round, suggested["id"]
        )
        document = interview_store.get_document(result["data"]["document_id"])
        assert "历史真题" in document["body"]
        assert "为什么选择 IPO 审核问询与定价风险" in document["body"]

        ai_round = interview_store.create_round(track_id, {
            "round_name": "AI 产品面", "round_type": "业务面",
        })
        interview_store.replace_exam(ai_round, [{
            "original_question": "你用过Cursor、Claude Code、Codex，你觉得Claude Code或者Codex比Cursor好在哪里？",
            "normalized_question": "Claude Code 或 Codex 相比 Cursor 好在哪里？",
            "intent": "验证 AI Coding 工具的使用深度与差异化理解",
            "answers": [],
        }])
        ai_question = interview_store.get_exam(ai_round)[0]
        interview_store.save_question_classification(ai_question["id"], {
            "question_type": "项目深挖",
            "ability_key": "事实与证据",
            "tags": [{"type": "追问角度", "value": "工具比较", "confirmed": True}],
            "links": [],
            "confirmed": False,
        })
        db.init_db()
        ai_question = interview_store.get_question(ai_question["id"])
        ai_topics = {
            item["tag_value"] for item in ai_question["tag_items"]
            if item["tag_type"] == "知识领域"
        }
        assert ai_question["question_type"] == "AI认知"
        assert ai_question["ability_key"] == "AI产品判断"
        assert {"AI与Agent", "AI工具"} <= ai_topics
        print("INTERVIEW_QUESTION_ASSETS_OK", suggested["id"], project_id)


if __name__ == "__main__":
    main()
