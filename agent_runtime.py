"""Expert registry routing for Caddie's unified Agent Runtime."""

import re

import db


ROUTING_RULES = {
    "review_analyst": (
        "复盘", "逐字稿", "录音", "面试表现", "面试官意图", "哪里答得", "回答问题",
    ),
    "pressure_interviewer": (
        "模拟面试", "开始模拟", "面试练习", "压力面", "预测追问", "你来问我", "mock interview",
    ),
    "experience_detective": (
        "深挖经历", "拷问经历", "项目细节", "证据不足", "贡献边界", "是不是我做", "经历梳理",
        "数字口径", "事实核对",
    ),
    "knowledge_coach": (
        "讲知识", "讲懂", "解释", "是什么", "为什么", "原理", "概念", "知识体系", "学习",
        "专业知识", "行业知识", "知识文档", "知识库", "怎么理解",
    ),
    "job_researcher": (
        "公司研究", "岗位研究", "jd", "职位描述", "岗位画像", "招聘要求", "公司情况", "业务线",
        "竞品", "行业归属",
    ),
}

# The standalone resume Agent is intentionally offline. Resume files remain
# manageable in Caddie, but new resume-editing Agent tasks must not be routed
# until format-preserving DOCX writes and visual verification are implemented.
DISABLED_EXPERT_KEYS = {"resume_editor"}

TASK_TYPES = {
    "career_lead": "career_planning",
    "experience_detective": "experience_discovery",
    "job_researcher": "job_research",
    "resume_editor": "resume_edit",
    "knowledge_coach": "knowledge_learning",
    "pressure_interviewer": "mock_interview",
    "review_analyst": "interview_review",
}

COMPLEX_MARKERS = ("整体", "系统", "全套", "从头", "规划一下", "一起完成", "多个", "全部", "先后顺序")


def _score(instruction, expert_key):
    text = instruction.lower()
    score = 0
    hits = []
    for keyword in ROUTING_RULES.get(expert_key, ()):
        count = text.count(keyword.lower())
        if count:
            score += 3 if len(keyword) >= 4 else 2
            hits.append(keyword)
    return score, hits


def coordinate(instruction, object_type=None, object_id=None, track_id=None):
    """Route one instruction to a primary expert without invoking a model."""
    instruction = re.sub(r"\s+", " ", (instruction or "").strip())
    experts = {item["key"]: item for item in db.list_agent_experts()}
    archive_markers = (
        "写入档案", "写入职业档案", "整理入库", "新建求职线", "新建一条求职线",
        "合并求职线", "更新进度", "更正状态", "针对写入前的确认",
    )
    if any(marker in instruction for marker in archive_markers):
        selected_key = "career_lead"
        expert = experts.get(selected_key) or {
            "key": selected_key, "name": selected_key, "system_prompt": "",
            "model_profile": "writing", "capabilities": [],
        }
        return {
            "expert_key": selected_key,
            "expert": expert,
            "reason": "当前任务涉及职业档案写入或状态纠正，由求职主理人保持上下文并负责确认",
            "confidence": 0.99,
            "task_type": TASK_TYPES[selected_key],
            "delegates": [],
            "scope": {"object_type": object_type, "object_id": object_id, "track_id": track_id},
        }
    object_defaults = {
        "knowledge_item": ("knowledge_coach", "当前任务直接作用于知识文档，由知识教练负责理解、整理和候选修改"),
        "project": ("experience_detective", "当前任务直接作用于项目经历，由经历侦探负责事实边界与证据审计"),
    }
    if object_type in object_defaults:
        selected_key, reason = object_defaults[object_type]
        expert = experts.get(selected_key) or {
            "key": selected_key, "name": selected_key, "system_prompt": "",
            "model_profile": "deep_reasoning", "capabilities": [],
        }
        return {
            "expert_key": selected_key,
            "expert": expert,
            "reason": reason,
            "confidence": 0.98,
            "task_type": TASK_TYPES.get(selected_key, "general"),
            "delegates": [],
            "scope": {"object_type": object_type, "object_id": object_id, "track_id": track_id},
        }
    ranked = []
    for key in ROUTING_RULES:
        if key in DISABLED_EXPERT_KEYS:
            continue
        score, hits = _score(instruction, key)
        if score:
            ranked.append((score, key, hits))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    matched_domains = [item for item in ranked if item[0] >= 2]
    is_complex = len(matched_domains) >= 2 and any(marker in instruction for marker in COMPLEX_MARKERS)
    if is_complex:
        selected_key = "career_lead"
        delegates = [item[1] for item in matched_domains[:3]]
        reason = "目标同时涉及多个专业环节，由主理人先拆解并安排协作顺序"
        confidence = min(0.96, 0.72 + len(delegates) * 0.07)
    elif ranked:
        top_score, selected_key, hits = ranked[0]
        delegates = []
        reason = f"识别到用户当前重点：{'、'.join(hits[:3])}"
        confidence = min(0.95, 0.58 + top_score * 0.055)
    else:
        selected_key = "career_lead"
        delegates = []
        reason = "当前目标尚未落入单一专业环节，由主理人先确认问题并给出推进方式"
        confidence = 0.62

    expert = experts.get(selected_key) or {
        "key": selected_key, "name": selected_key, "system_prompt": "",
        "model_profile": "deep_reasoning", "capabilities": [],
    }
    delegate_experts = [experts[key] for key in delegates if key in experts]
    return {
        "expert_key": selected_key,
        "expert": expert,
        "reason": reason,
        "confidence": round(confidence, 2),
        "task_type": TASK_TYPES.get(selected_key, "general"),
        "delegates": delegate_experts,
        "scope": {"object_type": object_type, "object_id": object_id, "track_id": track_id},
    }


def system_prompt_for(route):
    expert = route.get("expert") or {}
    prompt = expert.get("system_prompt") or ""
    identity = f"本轮由专家「{expert.get('name') or route.get('expert_key')}」主责。"
    if route.get("delegates"):
        names = "、".join(item.get("name") or item.get("key") for item in route["delegates"])
        identity += f" 可按顺序调用：{names}。先给出分工与第一步，不要假装这些专家已经完成工作。"
    return identity + "\n" + prompt
