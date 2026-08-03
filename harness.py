"""
Caddie Agent Harness.

Harness 的目标不是让模型更“会聊天”，而是把每次 AI 工作变成可复用、
可检查、可追溯的流程：输入规格、上下文预算、输出质量门、返修指令。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any


DEFAULT_CONTEXT_BUDGET = 32000

FEEDBACK_HARD_CATEGORIES = {"fact_correction", "ownership", "avoidance"}
FEEDBACK_CATEGORIES = {
    "fact_correction": "事实修正",
    "ownership": "贡献边界",
    "tone": "表达风格",
    "emphasis": "强调重点",
    "avoidance": "避雷禁区",
    "interviewer_probe": "面试追问",
}

READY_LEVELS = [
    (85, "ready", "基本可投递/可面试"),
    (70, "nearly_ready", "主材料可用，仍有明显补强点"),
    (50, "partial", "已有基础，但关键资产不足"),
    (0, "thin", "上下文偏薄，生成和面试风险较高"),
]


@dataclass
class HarnessIssue:
    level: str
    code: str
    message: str


@dataclass
class HarnessReport:
    stage: str
    score: int
    passed: bool
    issues: list[HarnessIssue]
    checks: dict[str, Any]
    repair_instruction: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["issues"] = [asdict(x) for x in self.issues]
        return data


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n...（Harness 已按上下文预算截断，原文约 {len(text)} 字）"


def pack_context(context_text: str, task: dict | None = None, budget: int = DEFAULT_CONTEXT_BUDGET) -> dict:
    """Keep important context sections while staying inside a deterministic budget."""
    text = context_text or ""
    task = task or {}
    sections = re.split(r"\n(?=## )", text)
    priority = ["生效反馈约束", "求职线", "项目事实", "岗位差距清单", "关联原始资料", "混合检索命中"]
    ranked = []
    for section in sections:
        title = section.splitlines()[0] if section.splitlines() else ""
        rank = next((i for i, key in enumerate(priority) if key in title), len(priority) + 1)
        ranked.append((rank, section))
    ranked.sort(key=lambda x: x[0])
    out, used = [], 0
    for _, section in ranked:
        section = section.strip()
        if not section:
            continue
        remain = budget - used
        if remain <= 500:
            break
        piece = _clip(section, remain)
        out.append(piece)
        used += len(piece)
    packed = "\n\n".join(out).strip()
    return {
        "text": packed,
        "budget": budget,
        "used": len(packed),
        "truncated": len(text) > len(packed),
        "task": {
            "id": task.get("id"),
            "type": task.get("task_type"),
            "expert": task.get("assigned_expert"),
            "object_type": task.get("object_type"),
            "object_id": task.get("object_id"),
            "track_id": task.get("track_id"),
        },
    }


def validate_task(task: dict, expert_key: str) -> HarnessReport:
    issues: list[HarnessIssue] = []
    instruction = (task.get("instruction") or "").strip()
    if len(instruction) < 4:
        issues.append(HarnessIssue("error", "instruction_too_short", "任务目标太短，模型无法判断交付物。"))
    if expert_key in {"resume_editor", "pressure_interviewer", "review_analyst", "job_researcher"} and not task.get("track_id"):
        issues.append(HarnessIssue("warning", "missing_track", "任务没有绑定求职线，上下文可能不完整。"))
    if expert_key == "experience_detective" and task.get("object_type") != "project":
        issues.append(HarnessIssue("error", "missing_project", "经历侦探必须绑定具体项目。"))
    if expert_key == "knowledge_coach" and not (task.get("track_id") or task.get("object_type") == "knowledge_item"):
        issues.append(HarnessIssue("warning", "missing_knowledge_scope", "知识任务没有明确作用域，可能难以沉淀复用。"))
    score = 100 - sum(30 if x.level == "error" else 10 for x in issues)
    score = max(0, score)
    return HarnessReport(
        stage="task_validation",
        score=score,
        passed=not any(x.level == "error" for x in issues),
        issues=issues,
        checks={"instruction_chars": len(instruction), "expert_key": expert_key},
    )


def _has_any(text: str, words: list[str]) -> bool:
    return any(w.lower() in (text or "").lower() for w in words)


def _status_from_ratio(ratio: float) -> str:
    if ratio >= 0.95:
        return "done"
    if ratio >= 0.55:
        return "partial"
    return "missing"


def _readiness_level(score: int) -> dict:
    for threshold, key, label in READY_LEVELS:
        if score >= threshold:
            return {"key": key, "label": label}
    return {"key": "thin", "label": "上下文偏薄，生成和面试风险较高"}


def _readiness_check(key: str, label: str, weight: int, ratio: float,
                     reason: str, action: str = "") -> dict:
    ratio = max(0.0, min(1.0, float(ratio or 0)))
    return {
        "key": key,
        "label": label,
        "weight": weight,
        "score": round(weight * ratio),
        "status": _status_from_ratio(ratio),
        "reason": reason,
        "action": action,
    }


def _asset_type_matches(asset: dict, keywords: list[str]) -> bool:
    text = " ".join(str(asset.get(k) or "") for k in ("asset_type", "title", "body")).lower()
    return any(k.lower() in text for k in keywords)


def _gap_completion(gaps: list[dict]) -> tuple[float, dict]:
    if not gaps:
        return 0.0, {"total": 0, "done": 0, "blockers": 0, "open": 0}
    total_weight = 0
    done_weight = 0
    blockers = 0
    open_count = 0
    for gap in gaps:
        severity = gap.get("severity") or "fixable"
        weight = 3 if severity == "blocker" else 2 if severity == "fixable" else 1
        total_weight += weight
        if gap.get("status") == "done":
            done_weight += weight
        else:
            open_count += 1
            if severity == "blocker":
                blockers += 1
    return (done_weight / total_weight if total_weight else 0.0), {
        "total": len(gaps),
        "done": sum(1 for x in gaps if x.get("status") == "done"),
        "blockers": blockers,
        "open": open_count,
    }


def compute_track_readiness(track: dict | None, *, gaps: list[dict] | None = None,
                            assets: list[dict] | None = None,
                            knowledge_items: list[dict] | None = None,
                            resume_versions: list[dict] | None = None,
                            feedback: list[dict] | None = None) -> dict:
    """Deterministic scorecard for a job track. No model calls, no writes."""
    track = track or {}
    gaps = gaps or []
    assets = assets or []
    knowledge_items = knowledge_items or []
    resume_versions = resume_versions or []
    feedback = feedback or []
    jd = (track.get("jd") or "").strip()
    persona = (track.get("persona") or track.get("target") or track.get("notes") or "").strip()
    submitted_resumes = [x for x in resume_versions if (x.get("status") or "") == "submitted"]
    editing_resumes = [x for x in resume_versions if (x.get("status") or "") != "submitted"]
    stale_assets = [x for x in assets if (x.get("status") or "").lower() == "stale"]
    active_assets = [x for x in assets if (x.get("status") or "").lower() != "stale"]
    intro_assets = [x for x in active_assets if _asset_type_matches(x, ["intro", "self", "自我介绍"])]
    resume_assets = [x for x in active_assets if _asset_type_matches(x, ["resume", "简历"])]
    pitch_assets = [x for x in active_assets if _asset_type_matches(x, ["pitch", "话术", "项目"])]
    mock_assets = [x for x in active_assets if _asset_type_matches(x, ["mock", "interview", "面试", "题库"])]
    knowledge_assets = [x for x in active_assets if _asset_type_matches(x, ["knowledge", "card", "知识卡"])]
    hard_feedback = [x for x in feedback if (x.get("strength") or "").lower() == "hard"]
    gap_ratio, gap_stats = _gap_completion(gaps)

    checks = [
        _readiness_check(
            "jd", "岗位 JD 与目标", 14,
            1 if len(jd) >= 120 else 0.6 if jd else 0,
            "JD 足够完整时，Caddie 才能围绕岗位要求装配上下文。",
            "补充完整 JD、岗位职责、任职要求和投递入口。" if not jd else "",
        ),
        _readiness_check(
            "positioning", "岗位定位/主线", 8,
            1 if len(persona) >= 40 else 0.5 if persona else 0,
            "同一个人投不同岗位，需要明确这条线主打什么能力。",
            "写一句该岗位的主打人设和 2-3 个证据。" if not persona else "",
        ),
        _readiness_check(
            "gaps", "差距清单闭环", 14,
            gap_ratio if gaps else 0,
            f"当前差距 {gap_stats['total']} 条，已完成 {gap_stats['done']} 条，未完成 {gap_stats['open']} 条。",
            "先生成 JD 对比，再把 blocker/fixable gap 转为行动。" if not gaps else "优先处理 blocker gap。" if gap_stats["blockers"] else "",
        ),
        _readiness_check(
            "resume", "岗位简历版本", 14,
            1 if submitted_resumes else 0.75 if editing_resumes else 0.65 if resume_assets else 0,
            f"已登记岗位简历 {len(resume_versions)} 版，其中已投递/定稿 {len(submitted_resumes)} 版。",
            "生成或登记一版针对该岗位的简历。" if not (resume_versions or resume_assets) else "",
        ),
        _readiness_check(
            "intro", "自我介绍/开场", 10,
            1 if intro_assets else 0,
            f"可用自我介绍资产 {len(intro_assets)} 个。",
            "生成 60 秒、90 秒两版自我介绍。" if not intro_assets else "",
        ),
        _readiness_check(
            "project_pitch", "项目话术", 12,
            1 if len(pitch_assets) >= 2 else 0.55 if pitch_assets else 0,
            f"可用项目话术/项目资产 {len(pitch_assets)} 个。",
            "围绕 JD 选择 2-3 个项目生成 STAR 话术和追问。" if len(pitch_assets) < 2 else "",
        ),
        _readiness_check(
            "knowledge", "知识卡/岗位知识", 10,
            1 if (knowledge_items or knowledge_assets) else 0,
            f"知识卡 {len(knowledge_items)} 条，知识类资产 {len(knowledge_assets)} 个。",
            "把 JD 缺口和面试真题沉淀成知识卡。" if not (knowledge_items or knowledge_assets) else "",
        ),
        _readiness_check(
            "mock", "模拟面试/复盘", 8,
            1 if mock_assets else 0,
            f"面试训练类资产 {len(mock_assets)} 个。",
            "做一轮针对该岗位的模拟面试，并沉淀复盘。" if not mock_assets else "",
        ),
        _readiness_check(
            "feedback", "反馈约束与资产新鲜度", 10,
            1 if not stale_assets else 0.45 if len(stale_assets) <= 2 else 0.15,
            f"活跃反馈 {len(feedback)} 条，其中 hard 约束 {len(hard_feedback)} 条；过期资产 {len(stale_assets)} 个。",
            "先处理 hard 反馈，并重生成 stale 资产。" if stale_assets else "",
        ),
    ]
    total = sum(x["weight"] for x in checks)
    score = round(sum(x["score"] for x in checks) * 100 / total) if total else 0
    blockers = [x for x in checks if x["status"] == "missing" and x["weight"] >= 10]
    next_actions = [x["action"] for x in checks if x.get("action")][:5]
    level = _readiness_level(score)
    return {
        "scope": "track",
        "track_id": track.get("id"),
        "score": score,
        "level": level,
        "summary": f"{track.get('company') or '这条求职线'} · {track.get('role') or '目标岗位'}：{level['label']}（{score} 分）。",
        "checks": checks,
        "blockers": blockers,
        "next_actions": next_actions,
        "signals": {
            "assets": len(assets),
            "stale_assets": len(stale_assets),
            "knowledge_items": len(knowledge_items),
            "resume_versions": len(resume_versions),
            "feedback": len(feedback),
            "gap_stats": gap_stats,
        },
    }


def compute_project_readiness(project: dict | None, *, followups: list[dict] | None = None,
                              assets: list[dict] | None = None,
                              sources: list[dict] | None = None) -> dict:
    """Deterministic scorecard for a project/story asset."""
    project = project or {}
    followups = followups or []
    assets = assets or []
    sources = sources or []
    document = (project.get("document") or "").strip()
    one_liner = (project.get("one_liner") or "").strip()
    tech = (project.get("technologies") or project.get("keywords") or "").strip()
    answered_followups = [x for x in followups if (x.get("answer") or "").strip()]
    active_assets = [x for x in assets if (x.get("status") or "").lower() != "stale"]
    pitch_assets = [x for x in active_assets if _asset_type_matches(x, ["pitch", "话术", "interview", "面试"])]
    has_background = _has_any(document, ["背景", "问题", "为什么", "目标"])
    has_action = _has_any(document, ["动作", "负责", "参与", "推动", "设计", "分析"])
    has_result = _has_any(document, ["结果", "提升", "降低", "完成", "%", "百分点", "产出"])
    checks = [
        _readiness_check(
            "one_liner", "一句话亮点", 12,
            1 if len(one_liner) >= 20 else 0.5 if one_liner else 0,
            "一句话亮点决定面试官是否愿意继续追问。",
            "补一句：我解决了什么问题，用了什么方法，带来什么结果。" if len(one_liner) < 20 else "",
        ),
        _readiness_check(
            "document_depth", "项目事实厚度", 18,
            1 if len(document) >= 1200 else 0.7 if len(document) >= 700 else 0.35 if document else 0,
            f"当前项目文档约 {len(document)} 字。",
            "补充背景、你的决策、关键数据、协作对象和交付物。" if len(document) < 700 else "",
        ),
        _readiness_check(
            "story_structure", "背景-动作-结果结构", 18,
            (sum([has_background, has_action, has_result]) / 3),
            f"背景/动作/结果覆盖：{sum([has_background, has_action, has_result])}/3。",
            "按“为什么做、我怎么拆、结果影响什么”补齐。" if not (has_background and has_action and has_result) else "",
        ),
        _readiness_check(
            "evidence", "证据与来源", 12,
            1 if sources else 0.5 if _has_any(document, ["数据", "口径", "样本", "指标", "SQL", "Wind"]) else 0,
            f"关联资料 {len(sources)} 份。",
            "把原始周报、文档、表格或截图关联为证据。" if not sources else "",
        ),
        _readiness_check(
            "followups", "追问题库", 16,
            1 if len(answered_followups) >= 5 else 0.7 if len(answered_followups) >= 3 else 0.35 if followups else 0,
            f"追问 {len(followups)} 条，已写答案 {len(answered_followups)} 条。",
            "至少准备 5 个高频追问：难点、取舍、协作、数据口径、复盘。" if len(answered_followups) < 5 else "",
        ),
        _readiness_check(
            "interview_asset", "面试话术资产", 12,
            1 if pitch_assets else 0,
            f"项目面试话术资产 {len(pitch_assets)} 个。",
            "生成 1 分钟版、STAR 版和深挖版话术。" if not pitch_assets else "",
        ),
        _readiness_check(
            "tools", "工具/方法可讲清", 12,
            1 if tech else 0.5 if _has_any(document, ["Excel", "SQL", "Python", "AI", "Wind", "模型", "Prompt"]) else 0,
            "项目需要能讲清方法论、工具和为什么这样做。",
            "补充工具链、分析框架和关键判断口径。" if not tech else "",
        ),
    ]
    total = sum(x["weight"] for x in checks)
    score = round(sum(x["score"] for x in checks) * 100 / total) if total else 0
    blockers = [x for x in checks if x["status"] == "missing" and x["weight"] >= 12]
    next_actions = [x["action"] for x in checks if x.get("action")][:5]
    level = _readiness_level(score)
    return {
        "scope": "project",
        "project_id": project.get("id"),
        "score": score,
        "level": level,
        "summary": f"{project.get('name') or '这个项目'}：{level['label']}（{score} 分）。",
        "checks": checks,
        "blockers": blockers,
        "next_actions": next_actions,
        "signals": {
            "document_chars": len(document),
            "followups": len(followups),
            "answered_followups": len(answered_followups),
            "assets": len(assets),
            "sources": len(sources),
        },
    }


def evaluate_output_case(text: str, *, title: str = "", expert_key: str = "career_lead",
                         mode: str = "answer", expected_keywords: list[str] | None = None,
                         min_score: int = 70) -> dict:
    """Reusable evaluation harness for regression tests and model comparisons."""
    expected_keywords = [x for x in (expected_keywords or []) if str(x).strip()]
    if mode == "document":
        report = audit_document(title or "未命名文档", text, expert_key)
    else:
        report = audit_answer(text, expert_key, mode=mode)
    missing_keywords = [kw for kw in expected_keywords if kw not in (text or "")]
    score = report.score
    issues = list(report.issues)
    if missing_keywords:
        penalty = min(25, 6 * len(missing_keywords))
        score = max(0, score - penalty)
        issues.append(HarnessIssue(
            "warning", "missing_expected_keywords",
            "缺少预期关键词：" + "、".join(missing_keywords[:8]),
        ))
    passed = not any(x.level == "error" for x in issues) and score >= min_score
    return {
        "title": title,
        "expert_key": expert_key,
        "mode": mode,
        "score": score,
        "passed": passed,
        "min_score": min_score,
        "missing_keywords": missing_keywords,
        "base_report": report.to_dict(),
        "issues": [asdict(x) for x in issues],
        "repair_instruction": repair_instruction(issues) if issues else "",
    }


def plan_source_ingestion(candidates: list[dict], *, max_batch: int = 100) -> dict:
    """Preflight report before local candidates become durable sources."""
    candidates = candidates or []
    issues: list[HarnessIssue] = []
    actions = []
    stats = {
        "total": len(candidates),
        "importable": 0,
        "duplicates": 0,
        "already_imported": 0,
        "missing_file": 0,
        "low_confidence": 0,
        "errors": 0,
    }
    if not candidates:
        issues.append(HarnessIssue("error", "empty_ingestion_batch", "没有选择要入库的候选资料。"))
    if len(candidates) > max_batch:
        issues.append(HarnessIssue("warning", "large_ingestion_batch", f"一次入库 {len(candidates)} 份资料，建议分批确认。"))
    for item in candidates[:max_batch]:
        status = item.get("status") or "candidate"
        confidence = float(item.get("confidence") or 0)
        title = item.get("title") or item.get("file_name") or item.get("file_path") or f"候选 #{item.get('id')}"
        action = {
            "candidate_id": item.get("id"),
            "title": title,
            "source_type": item.get("source_type") or "other",
            "confidence": confidence,
            "status": status,
            "action": "import",
            "reason": "候选资料可入库",
            "risks": [],
        }
        if status == "imported" or item.get("matched_source_id"):
            action.update({"action": "skip", "reason": "已导入", "source_id": item.get("matched_source_id")})
            stats["already_imported"] += 1
        elif item.get("duplicate_source_id") or status == "duplicate":
            action.update({"action": "skip", "reason": "资料库已有相同内容", "source_id": item.get("duplicate_source_id")})
            stats["duplicates"] += 1
        elif status == "error":
            action.update({"action": "error", "reason": item.get("error") or "候选状态异常"})
            stats["errors"] += 1
        else:
            stats["importable"] += 1
            if confidence and confidence < 0.45:
                action["risks"].append("AI/规则识别置信度偏低，建议先人工确认类型和标题。")
                stats["low_confidence"] += 1
            if not (item.get("sample_text") or "").strip():
                action["risks"].append("没有可预览文本，导入后可能仍需单独解析。")
        actions.append(action)
    if stats["low_confidence"]:
        issues.append(HarnessIssue("warning", "low_confidence_candidates", f"{stats['low_confidence']} 份候选置信度偏低。"))
    if stats["duplicates"]:
        issues.append(HarnessIssue("warning", "duplicate_candidates", f"{stats['duplicates']} 份候选将因重复跳过。"))
    score = 100
    for item in issues:
        score -= 35 if item.level == "error" else 10
    score = max(0, score)
    report = HarnessReport(
        stage="source_ingestion_preflight",
        score=score,
        passed=not any(x.level == "error" for x in issues),
        issues=issues,
        checks=stats,
        repair_instruction="请先选择可入库候选，并处理重复、低置信度或异常文件。" if issues else "",
    )
    return {
        "report": report.to_dict(),
        "stats": stats,
        "actions": actions,
        "principle": "扫描候选只代表 Caddie 找到了资料；只有用户确认导入后才会写入 sources。",
    }


def audit_answer(text: str, expert_key: str, mode: str = "answer") -> HarnessReport:
    issues: list[HarnessIssue] = []
    clean = (text or "").strip()
    checks = {
        "chars": len(clean),
        "has_next_step": _has_any(clean, ["下一步", "行动", "建议", "优先级", "计划"]),
        "has_evidence_language": _has_any(clean, ["依据", "来源", "证据", "已确认", "待确认", "不能确定", "推断"]),
        "has_structure": bool(re.search(r"(^|\n)(#{1,3}\s|[-*]\s|\d+[.、])", clean)),
    }
    if len(clean) < 180:
        issues.append(HarnessIssue("error", "too_short", "输出过短，无法作为可复用成果。"))
    if not checks["has_structure"]:
        issues.append(HarnessIssue("warning", "weak_structure", "输出缺少清晰结构，用户后续难以复用。"))
    if expert_key in {"resume_editor", "experience_detective", "review_analyst"} and not checks["has_evidence_language"]:
        issues.append(HarnessIssue("warning", "missing_evidence_boundary", "没有明显区分事实、证据与待确认内容。"))
    if expert_key in {"career_lead", "job_researcher"} and not checks["has_next_step"]:
        issues.append(HarnessIssue("warning", "missing_next_step", "没有给出下一步行动或优先级。"))
    if re.search(r"(一定能|保证拿到|百分百|绝对可以)", clean):
        issues.append(HarnessIssue("warning", "overpromise", "出现过度承诺表述，应改为概率、条件和依据。"))
    score = 100
    for item in issues:
        score -= 35 if item.level == "error" else 12
    score = max(0, score)
    repair = ""
    if issues:
        repair = repair_instruction(issues)
    return HarnessReport(
        stage=f"output_audit:{mode}",
        score=score,
        passed=not any(x.level == "error" for x in issues) and score >= 70,
        issues=issues,
        checks=checks,
        repair_instruction=repair,
    )


def _route_key(route: dict | None) -> str:
    return ((route or {}).get("expert_key") or "career_lead").strip() or "career_lead"


def _deliverable_for(expert_key: str, mode: str = "general") -> str:
    if expert_key == "resume_editor":
        return "简历/经历表达修改建议，必须包含证据边界和待确认事实。"
    if expert_key == "experience_detective":
        return "项目经历深挖与追问清单，必须区分事实、推断和缺口。"
    if expert_key == "job_researcher":
        return "岗位/公司匹配判断，必须包含依据、风险和准备优先级。"
    if expert_key == "knowledge_coach":
        return "把概念讲懂并沉淀为可复用知识结构。"
    if expert_key == "pressure_interviewer":
        return "模拟面试问题、考察意图和训练路径。"
    if expert_key == "review_analyst":
        return "面试复盘报告，必须还原问题、识别意图并给出训练动作。"
    if mode == "plan":
        return "可执行计划，必须包含优先级、完成标准和下一步。"
    if mode == "review":
        return "复盘结论，必须包含事实、原因、改进动作和复用经验。"
    return "直接回答用户问题，并给出依据与下一步。"


def validate_chat_request(message: str, route: dict | None = None, *,
                          attachment_count: int = 0, local_context_count: int = 0,
                          track_id: int | None = None, mode: str = "general") -> HarnessReport:
    """Quality gate before an interactive Ask Caddie turn is sent to a model."""
    issues: list[HarnessIssue] = []
    text = (message or "").strip()
    expert_key = _route_key(route)
    if not text and not (attachment_count or local_context_count):
        issues.append(HarnessIssue("error", "empty_chat_request", "没有问题、附件或本地上下文。"))
    if not text and local_context_count:
        issues.append(HarnessIssue("warning", "context_without_goal", "只挂载了本地资料，但没有说明要完成什么任务。"))
    if attachment_count + local_context_count > 20:
        issues.append(HarnessIssue("warning", "too_many_inputs", "本轮挂载资料过多，模型可能抓不住重点。"))
    if expert_key in {"resume_editor", "job_researcher", "pressure_interviewer", "review_analyst"} and not track_id:
        issues.append(HarnessIssue("warning", "no_track_scope", "任务可能需要岗位范围，但当前没有绑定求职线。"))
    if mode == "plan" and len(text) < 8:
        issues.append(HarnessIssue("warning", "plan_goal_vague", "规划目标较短，计划可能偏泛。"))
    score = 100
    for item in issues:
        score -= 35 if item.level == "error" else 10
    score = max(0, score)
    return HarnessReport(
        stage="chat_request_validation",
        score=score,
        passed=not any(x.level == "error" for x in issues),
        issues=issues,
        checks={
            "message_chars": len(text),
            "attachment_count": attachment_count,
            "local_context_count": local_context_count,
            "track_id": track_id,
            "mode": mode,
            "expert_key": expert_key,
            "route_confidence": (route or {}).get("confidence"),
        },
        repair_instruction="请先补充你希望 Caddie 完成的具体任务。" if issues else "",
    )


def build_chat_contract(route: dict | None = None, *, mode: str = "general",
                        track_id: int | None = None, local_context_count: int = 0,
                        attachment_count: int = 0, packed_context: dict | None = None) -> str:
    """Create a deterministic execution contract for Ask Caddie."""
    expert_key = _route_key(route)
    expert_name = ((route or {}).get("expert") or {}).get("name") or expert_key
    deliverable = _deliverable_for(expert_key, mode)
    used = (packed_context or {}).get("used", 0)
    budget = (packed_context or {}).get("budget", DEFAULT_CONTEXT_BUDGET)
    truncated = bool((packed_context or {}).get("truncated"))
    return "\n".join([
        "【Caddie Harness 执行契约】",
        f"- 主责专家：{expert_name}（{expert_key}）",
        f"- 工作模式：{mode}",
        f"- 交付物：{deliverable}",
        f"- 范围：{'岗位 #' + str(track_id) if track_id else '全局职业资料'}",
        f"- 本轮临时本地资料：{local_context_count} 份；上传附件：{attachment_count} 份",
        f"- 已装配上下文预算：{used}/{budget} 字；是否截断：{truncated}",
        "质量要求：",
        "1. 先给结论，再给依据；不要把用户已有背景完整复述一遍。",
        "2. 明确区分已确认事实、合理推断、待确认信息。",
        "3. 不得声称已经写库、保存、投递、修改本地文件，除非用户确认并有专门接口完成。",
        "4. 使用本地资料时要说明依据来自“本轮临时本地资料”或 Caddie 已有上下文。",
        "5. 最后给出下一步行动或建议用户确认的信息。",
    ])


def audit_chat_answer(text: str, route: dict | None = None, *,
                      mode: str = "general", local_context_count: int = 0,
                      attachment_count: int = 0,
                      response_style: str = "standard") -> HarnessReport:
    expert_key = _route_key(route)
    base = audit_answer(text, expert_key, mode=f"chat:{mode}")
    issues = list(base.issues)
    if response_style == "concise":
        issues = [
            item for item in issues
            if item.code not in {"too_short", "weak_structure"}
        ]
    clean = (text or "").strip()
    checks = dict(base.checks)
    checks.update({
        "mentions_local_context": _has_any(clean, ["本轮临时", "本地资料", "附件", "资料中", "根据你提供"]),
        "forbidden_persistence_claim": bool(re.search(r"(已保存|已经入库|已写入|已更新资料库|已修改本地|已经投递)", clean)),
        "local_context_count": local_context_count,
        "attachment_count": attachment_count,
        "response_style": response_style,
    })
    if local_context_count and not checks["mentions_local_context"]:
        issues.append(HarnessIssue("warning", "missing_local_context_attribution", "回答使用了本地资料，但没有提示资料来源边界。"))
    if checks["forbidden_persistence_claim"]:
        issues.append(HarnessIssue("error", "forbidden_persistence_claim", "回答声称已保存/入库/修改文件，违反 Caddie 写库确认规则。"))
    score = 100
    for item in issues:
        score -= 35 if item.level == "error" else 12
    score = max(0, score)
    return HarnessReport(
        stage=f"chat_output_audit:{mode}",
        score=score,
        passed=not any(x.level == "error" for x in issues) and score >= 70,
        issues=issues,
        checks=checks,
        repair_instruction=repair_instruction(issues) if issues else "",
    )


def normalize_feedback_note(original_text: str, *, scope: str = "global",
                            scope_id: int | None = None, category: str | None = None,
                            polarity: str | None = None, strength: str | None = None,
                            directive: str | None = None) -> dict:
    """Turn user feedback into a reusable constraint without calling a model."""
    raw = re.sub(r"\s+", " ", (original_text or "").strip())
    lowered = raw.lower()
    inferred_category = category
    if not inferred_category:
        if _has_any(raw, ["事实不对", "不是事实", "数据不对", "数字不对", "应该是", "实际是"]):
            inferred_category = "fact_correction"
        elif _has_any(raw, ["不是我做", "不是我负责", "不是我主导", "参与", "协助", "贡献边界"]):
            inferred_category = "ownership"
        elif _has_any(raw, ["不要", "别", "禁止", "不能写", "不能说", "避开", "少写"]):
            inferred_category = "avoidance"
        elif _has_any(raw, ["太虚", "不像我", "太夸张", "语气", "口语", "专业一点", "自然一点"]):
            inferred_category = "tone"
        elif _has_any(raw, ["更强调", "重点", "突出", "主线", "角度", "定位"]):
            inferred_category = "emphasis"
        elif _has_any(raw, ["追问", "面试官问", "被问到", "可能问"]):
            inferred_category = "interviewer_probe"
        else:
            inferred_category = "emphasis"
    if inferred_category not in FEEDBACK_CATEGORIES:
        inferred_category = "emphasis"

    inferred_polarity = polarity
    if not inferred_polarity:
        inferred_polarity = "avoid" if inferred_category in {"avoidance"} or _has_any(raw, ["不要", "别", "禁止", "不能"]) else "do"
    if inferred_polarity not in {"do", "avoid"}:
        inferred_polarity = "do"

    inferred_strength = strength
    if not inferred_strength:
        inferred_strength = "hard" if inferred_category in FEEDBACK_HARD_CATEGORIES else "soft"
    if inferred_strength not in {"hard", "soft"}:
        inferred_strength = "soft"

    final_directive = re.sub(r"\s+", " ", (directive or raw).strip())
    if not final_directive:
        final_directive = raw
    if inferred_polarity == "avoid" and not _has_any(final_directive, ["不要", "禁止", "避免", "不能"]):
        final_directive = "避免：" + final_directive
    elif inferred_polarity == "do" and inferred_category in {"fact_correction", "ownership"} and not _has_any(final_directive, ["事实", "边界", "应"]):
        final_directive = "事实/贡献边界：" + final_directive

    issues: list[HarnessIssue] = []
    if len(raw) < 4:
        issues.append(HarnessIssue("error", "feedback_too_short", "反馈内容太短，无法转成有效约束。"))
    if scope not in {"global", "track", "project", "asset", "experience", "interview"}:
        issues.append(HarnessIssue("warning", "unknown_scope", "反馈作用域不常见，后续影响范围可能需要人工确认。"))
    if scope != "global" and not scope_id:
        issues.append(HarnessIssue("warning", "missing_scope_id", "非全局反馈缺少 scope_id，将暂按文字约束保存。"))
    score = 100
    for item in issues:
        score -= 35 if item.level == "error" else 10
    report = HarnessReport(
        stage="feedback_normalization",
        score=max(0, score),
        passed=not any(x.level == "error" for x in issues),
        issues=issues,
        checks={
            "scope": scope,
            "scope_id": scope_id,
            "category": inferred_category,
            "polarity": inferred_polarity,
            "strength": inferred_strength,
            "chars": len(raw),
        },
        repair_instruction="请补充更具体的反馈，例如：哪个事实不对、以后应该怎么写、适用于哪个岗位/项目。" if issues else "",
    )
    return {
        "scope": scope or "global",
        "scope_id": scope_id,
        "category": inferred_category,
        "category_label": FEEDBACK_CATEGORIES[inferred_category],
        "polarity": inferred_polarity,
        "strength": inferred_strength,
        "directive": final_directive[:1000],
        "content": final_directive[:1000],
        "original_text": raw,
        "report": report,
    }


def feedback_impact_policy(normalized: dict, impacted_assets: list[dict] | None = None) -> dict:
    """Explain what should become stale after a feedback note is accepted."""
    impacted_assets = impacted_assets or []
    scope = normalized.get("scope") or "global"
    category = normalized.get("category") or "emphasis"
    strength = normalized.get("strength") or "soft"
    should_stale = False
    reason = "soft_feedback_record_only"
    if strength == "hard" or category in FEEDBACK_HARD_CATEGORIES:
        should_stale = True
        reason = "hard_constraint_affects_existing_assets"
    elif scope in {"track", "project", "asset"}:
        should_stale = True
        reason = "scoped_preference_affects_related_assets"
    return {
        "should_mark_stale": should_stale,
        "reason": reason,
        "scope": scope,
        "category": category,
        "strength": strength,
        "impacted_count": len(impacted_assets),
        "impacted_assets": [
            {
                "id": x.get("id"),
                "asset_type": x.get("asset_type"),
                "title": x.get("title"),
                "status": x.get("status"),
                "track_id": x.get("track_id"),
                "project_id": x.get("project_id"),
            }
            for x in impacted_assets[:30]
        ],
    }


def audit_document(title: str, body: str, expert_key: str) -> HarnessReport:
    issues: list[HarnessIssue] = []
    body = (body or "").strip()
    checks = {
        "title_chars": len((title or "").strip()),
        "body_chars": len(body),
        "heading_count": len(re.findall(r"(?m)^#{1,3}\s+", body)),
        "bullet_count": len(re.findall(r"(?m)^[-*]\s+", body)),
        "has_todo": _has_any(body, ["待确认", "下一步", "行动", "缺口", "风险"]),
        "has_evidence": _has_any(body, ["证据", "依据", "来源", "口径", "已确认", "推断"]),
    }
    if checks["title_chars"] < 4:
        issues.append(HarnessIssue("error", "missing_title", "缺少可展示标题。"))
    if checks["body_chars"] < 500:
        issues.append(HarnessIssue("error", "document_too_short", "正文太短，不适合作为资产沉淀。"))
    if checks["heading_count"] < 2:
        issues.append(HarnessIssue("warning", "few_sections", "正文分节不足，后续检索和复用会变差。"))
    if expert_key in {"resume_editor", "review_analyst"} and not checks["has_evidence"]:
        issues.append(HarnessIssue("warning", "missing_evidence", "文档没有明显证据边界。"))
    if not checks["has_todo"]:
        issues.append(HarnessIssue("warning", "missing_actions", "文档没有沉淀后续行动或待确认项。"))
    score = 100
    for item in issues:
        score -= 35 if item.level == "error" else 10
    score = max(0, score)
    return HarnessReport(
        stage="document_audit",
        score=score,
        passed=not any(x.level == "error" for x in issues) and score >= 70,
        issues=issues,
        checks=checks,
        repair_instruction=repair_instruction(issues) if issues else "",
    )


def repair_instruction(issues: list[HarnessIssue]) -> str:
    lines = ["请根据以下质量问题重写上一版输出，只返回修复后的最终内容："]
    for item in issues:
        lines.append(f"- [{item.level}] {item.code}: {item.message}")
    lines.extend([
        "硬性要求：",
        "1. 区分已确认事实、合理推断和待确认信息。",
        "2. 给出可执行下一步，避免空泛结论。",
        "3. 不编造用户贡献、项目数据、公司事实或面试结果。",
        "4. 用清晰标题和分节组织，便于后续沉淀为资产。",
    ])
    return "\n".join(lines)


def report_event_payload(report: HarnessReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False)
