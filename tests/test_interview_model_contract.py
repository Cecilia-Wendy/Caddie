import interview_decompose


PARTICIPANTS = [
    {"id": 1, "role": "self", "display_name": "候选人"},
    {"id": 2, "role": "interviewer", "display_name": "面试官"},
]

SEGMENTS = [
    {"id": 10, "raw_text": "你如何保证每周五天稳定实习？"},
    {"id": 11, "raw_text": "课程已经结束，可以每周五天到岗，毕业论文与实习并行。"},
    {"id": 12, "raw_text": "如果导师不同意你的判断，你会怎么处理？"},
    {"id": 13, "raw_text": "如果有客观证据我会调整；如果只是经验分歧，我会先做低成本验证。"},
    {"id": 14, "raw_text": "PMO首先要理解流程，其次建设协同机制，最后提升组织效能。"},
]


STRICT_OUTPUT = """
<<<OVERVIEW>>>
SUMMARY: 面试官先确认稳定性，再验证候选人的决策方式。
JUDGMENT_PATH: 投入条件→工作方式
<<<END>>>
<<<STAGE>>>
NAME: 投入条件
GOAL: 判断候选人能否稳定到岗
EVIDENCE: 连续确认课程和论文安排
<<<END>>>
<<<QUESTION>>>
STAGE: 投入条件
QUESTION: 你如何保证每周五天稳定实习？
TYPE: 基础信息
ABILITY: 稳定性
INTENT: 排除学业影响实习的风险
ANSWER:
课程已经结束，可以每周五天到岗，毕业论文与实习并行。
FOLLOW_UPS:
毕业论文是否影响出勤？
INTERVIEWER_SIGNAL:
面试官确认学业安排。
SOURCE_QUOTE:
课程已经结束｜每周五天到岗
<<<END>>>
<<<QUESTION>>>
STAGE: 工作方式
QUESTION: 如果导师不同意你的判断，你会怎么处理？
TYPE: 行为面试
ABILITY: 分歧处理
INTENT: 判断候选人能否兼顾独立判断和推进效率
ANSWER:
如果有客观证据我会调整；如果只是经验分歧，我会先做低成本验证。
FOLLOW_UPS:
你更倾向坚持还是直接接受？
INTERVIEWER_SIGNAL:
面试官追问候选人的决策依据。
SOURCE_QUOTE:
客观证据｜低成本验证
<<<END>>>
<<<QUESTION>>>
STAGE: 候选人反问
QUESTION: 你认为PMO最重要的能力是什么？
TYPE: 反问
ABILITY: 岗位判断
INTENT: 了解岗位能力模型
ANSWER:
FOLLOW_UPS:
INTERVIEWER_SIGNAL:
PMO首先要理解流程，其次建设协同机制，最后提升组织效能。
SOURCE_QUOTE:
流程｜协同机制｜组织效能
<<<END>>>
<<<QUESTION>>>
STAGE: 岗位理解
QUESTION: 你如何理解完整的项目管理流程？
TYPE: 业务理解
ABILITY: 项目管理
INTENT: 判断是否理解流程和里程碑
ANSWER:
我会先定义问题和目标，再明确责任人、依赖和时间节点，最后完成验收与复盘。
FOLLOW_UPS:
有哪些关键里程碑？
INTERVIEWER_SIGNAL:
面试官要求结合真实项目说明。
SOURCE_QUOTE:
定义问题｜时间节点
<<<END>>>
<<<ROLE_INSIGHT>>>
TITLE: PMO能力模型
CONTENT: PMO需要理解流程、建设协同机制并提升组织效能。
SOURCE_QUOTE: 流程｜协同机制｜组织效能
<<<END>>>
"""


LOCALISED_OUTPUT = STRICT_OUTPUT.replace("<<<OVERVIEW>>>", "<<<概览>>>").replace(
    "<<<STAGE>>>", "<<<阶段>>>"
).replace("<<<QUESTION>>>", "<<<问题>>>").replace(
    "<<<ROLE_INSIGHT>>>", "<<<岗位认知>>>"
).replace("<<<END>>>", "<<<结束>>>").replace(
    "QUESTION:", "问题："
).replace("ANSWER:", "回答：").replace(
    "FOLLOW_UPS:", "追问："
).replace("INTERVIEWER_SIGNAL:", "面试官反馈：")


def test_contract_is_stable_across_provider_styles():
    first = interview_decompose.parse_ai_exam_bundle(STRICT_OUTPUT, SEGMENTS, PARTICIPANTS)
    second = interview_decompose.parse_ai_exam_bundle(LOCALISED_OUTPUT, SEGMENTS, PARTICIPANTS)
    assert [x["normalized_question"] for x in first["questions"]] == [
        x["normalized_question"] for x in second["questions"]
    ]
    assert first["role_insights"][0]["title"] == second["role_insights"][0]["title"]
    assert first["questions"][2]["answers"] == []


def test_quality_gate_accepts_clean_reconstruction():
    bundle = interview_decompose.parse_ai_exam_bundle(STRICT_OUTPUT, SEGMENTS, PARTICIPANTS)
    result = interview_decompose.validate_exam_bundle(bundle)
    assert result["accepted"] is True
    assert result["answer_ratio"] >= 0.6


def test_quality_gate_rejects_interviewer_leakage():
    contaminated = STRICT_OUTPUT.replace(
        "课程已经结束，可以每周五天到岗，毕业论文与实习并行。",
        "你这边能给我介绍一下吗？您是否可以每周五天到岗？",
        1,
    )
    bundle = interview_decompose.parse_ai_exam_bundle(contaminated, SEGMENTS, PARTICIPANTS)
    result = interview_decompose.validate_exam_bundle(bundle)
    assert result["accepted"] is False
    assert any("面试官话语" in item for item in result["fatal"])


def test_provider_commentary_is_not_promoted_to_question():
    commentary = """
<<<QUESTION>>>
STAGE: 项目深挖
QUESTION: （面试官反馈与补充）
TYPE: 其他
ABILITY: 待确认
INTENT: 待确认
ANSWER:
FOLLOW_UPS:
INTERVIEWER_SIGNAL: 回答还要补充验收和复盘。
SOURCE_QUOTE: 验收和复盘
<<<END>>>
"""
    bundle = interview_decompose.parse_ai_exam_bundle(
        STRICT_OUTPUT + commentary, SEGMENTS, PARTICIPANTS
    )
    assert len(bundle["questions"]) == 4
    assert all("面试官反馈" not in item["normalized_question"] for item in bundle["questions"])


def test_long_transcript_requires_broader_question_coverage():
    bundle = interview_decompose.parse_ai_exam_bundle(STRICT_OUTPUT, SEGMENTS, PARTICIPANTS)
    result = interview_decompose.validate_exam_bundle(bundle, "长逐字稿" * 2500)
    assert result["accepted"] is False
    assert result["minimum_question_count"] == 5
