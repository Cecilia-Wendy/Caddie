"""Regression test for cross-interview growth analysis and training outputs."""
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db
import interview_store
import server


def _round(company, role, scheduled_at, questions):
    track_id = db.create_job_track({"company": company, "role": role, "jd": role})
    round_id = interview_store.create_round(track_id, {
        "round_name": "业务面", "round_type": "professional",
        "scheduled_at": scheduled_at, "status": "completed",
    })
    interview_store.ensure_round_documents(round_id)
    interview_store.replace_exam(round_id, [{
        "original_question": text,
        "normalized_question": text,
        "question_type": question_type,
        "ability_key": ability,
        "intent": intent,
        "confirmed": True,
        "answers": [{
            "is_self": True,
            "original_answer": answer,
            "organized_answer": answer,
            "confirmed": True,
        }],
    } for text, question_type, ability, intent, answer in questions])
    return track_id, round_id


def main():
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = Path(tmp) / "caddie.db"
        server._commit = lambda _message: None
        db.init_db()

        _, first_round = _round("甲公司", "AI产品经理", "2026-05-01 10:00", [
            ("你如何判断一个 AI 功能是否值得做？", "AI认知", "产品判断", "验证产品判断", "我会从用户价值和成本判断。"),
            ("请介绍你的 AI 工作流项目。", "项目深挖", "项目表达", "验证项目深度", "我先拆需求，再设计工作流。"),
        ])
        _, second_round = _round("乙公司", "AI产品经理实习生", "2026-07-01 10:00", [
            ("你如何衡量 AI 产品是否真正有价值？", "AI认知", "产品判断", "验证产品判断", "我会看任务成功率、采纳率和人工回退率。"),
            ("为什么 Agent 不能只是一个聊天框？", "AI认知", "产品设计", "验证 Agent 理解", "Agent 还需要状态、工具、反馈和可确认产物。"),
        ])
        _round("丙公司", "债券交易实习生", "2026-06-01 10:00", [
            ("久期是什么？", "专业知识", "固收基础", "验证金融知识", "久期衡量价格对利率的敏感度。"),
        ])

        candidates = server._growth_scope_candidates("AI产品经理")
        assert {item["id"] for item in candidates} == {first_round, second_round}, candidates

        analysis_id = interview_store.create_growth_analysis({
            "title": "AI产品经理综合分析与训练",
            "role_family": "AI产品经理",
            "role_subtype": "Agent",
            "selected_round_ids": [first_round, second_round],
            "target_jd": "负责企业 Agent 产品规划、评测与落地",
            "status": "queued",
        })
        task_id = db.create_agent_task({
            "task_type": "interview_growth_analysis",
            "title": "AI产品经理综合分析与训练",
            "object_type": "interview_growth_analysis",
            "object_id": analysis_id,
            "status": "queued",
            "assigned_expert": "review_analyst",
        })

        original_chat = server.ai.chat
        server.ai.chat = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        try:
            server._run_interview_growth_task(task_id, analysis_id, mock_count=4)
        finally:
            server.ai.chat = original_chat

        analysis = interview_store.get_growth_analysis(analysis_id)
        assert analysis["status"] == "completed", analysis
        assert analysis["progress"] == 100
        assert len(analysis["source_snapshot"]["rounds"]) == 2
        assert db.get_agent_task(task_id)["status"] == "completed"

        expected = {
            "profile": "面试能力画像",
            "trend": "面试时间趋势",
            "preparation": "面试准备计划",
            "mock": "模拟面试卷",
            "answer_sheet": "模拟答卷",
            "reference": "参考答案",
        }
        for key, phrase in expected.items():
            document = analysis["documents"][key]
            assert document and document["editable"], (key, document)
            assert phrase in document["title"] or phrase in document["body"], (key, document)
        assert "甲公司" in analysis["documents"]["profile"]["body"]
        assert "乙公司" in analysis["documents"]["trend"]["body"]
        assert "负责企业 Agent" in analysis["documents"]["preparation"]["body"]

        mock_replies = iter([
            "你好，我们开始。请先用两分钟介绍一个最能体现你 AI 产品判断力的项目。",
            "你提到先判断用户价值。请具体说明你当时用什么证据决定这个需求值得做？",
            """# AI产品经理模拟面试答卷

## 一、本轮整体表现

候选人能够提出用户价值和成本两个判断维度，但关键证据仍需补充。

## 二、逐题答卷

### Q1 面试官问题

请介绍一个最能体现 AI 产品判断力的项目。

**我的真实回答**

我先判断用户价值，再结合实现成本决定是否推进。

**回答诊断**

方向正确，但缺少真实项目证据。

**参考答法**

待补事实后再形成。

## 三、反复暴露的薄弱点

- 证据不足。

## 四、下一轮训练清单

- 补充一次真实需求取舍案例。
""",
        ])
        server.ai.chat = lambda *_args, **_kwargs: next(mock_replies)
        started = server.start_interview_growth_mock_v2(analysis_id, reset=False)["data"]
        assert started["status"] == "active", started
        assert started["answer_count"] == 0
        assert len(started["messages"]) == 1
        assert started["messages"][0]["role"] == "assistant"

        responded = server.respond_interview_growth_mock_v2(
            analysis_id,
            server.InterviewGrowthMockMessageIn(
                message="我先判断用户价值，再结合实现成本决定是否推进。"
            ),
        )["data"]
        assert responded["answer_count"] == 1, responded
        assert [item["role"] for item in responded["messages"]] == [
            "assistant", "user", "assistant",
        ]

        restored = server.get_interview_growth_mock_v2(analysis_id)["data"]
        assert restored["messages"] == responded["messages"]
        assert "什么证据" in restored["messages"][-1]["content"]

        finished = server.finish_interview_growth_mock_v2(analysis_id)["data"]
        assert finished["status"] == "completed", finished
        assert finished["document"]["editable"]
        refreshed = interview_store.get_growth_analysis(analysis_id)
        answer_sheet = refreshed["documents"]["answer_sheet"]
        assert "逐题答卷" in answer_sheet["body"]
        assert "用户价值" in answer_sheet["body"]

        print("INTERVIEW_GROWTH_OK", analysis_id, analysis["summary"])


if __name__ == "__main__":
    main()
