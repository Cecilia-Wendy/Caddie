"""Compare configured text models against the interview reconstruction contract.

Usage:
    .venv/bin/python scripts/validate_interview_models.py ROUND_ID SOURCE_ID
    .venv/bin/python scripts/validate_interview_models.py --fixture

This is a read-only diagnostic. It never replaces the saved exam.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ai
import db
import interview_decompose
import interview_store


FIXTURE = """会议转写原文
会议所有发言都被错误标记为“参会人”。
参会人：你能保证每周五天稳定到岗吗？
参会人：课程已经结束，我可以每周五天到岗，毕业论文与实习并行。
参会人：介绍一个你推动过的跨团队项目，重点说你如何处理分歧。
参会人：项目最初目标模糊，我先拉齐业务、产品、研发和测试定义成功标准，再明确责任人与节点。出现方案分歧时，我先确认双方依据；如果没有充分证据，就用一天完成小范围验证，再决定方向。
参会人：这个回答体现了验证意识，但完整项目管理还要覆盖依赖、风险、验收和复盘。
参会人：我想反问，AI在项目管理中最值得优先应用在哪里？
参会人：先梳理协作动线中的信息流和决策流，找到ROI最高的单点，用原型验证后再工程化。
"""


def _fixture_inputs():
    participants = [
        {"id": 1, "role": "self", "display_name": "候选人"},
        {"id": 2, "role": "interviewer", "display_name": "面试官"},
    ]
    segments = [
        {"id": index, "raw_text": line}
        for index, line in enumerate(FIXTURE.splitlines(), 1) if line.strip()
    ]
    return FIXTURE, segments, participants, "岗位：产研项目管理实习生"


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--fixture":
        raw_text, segments, participants, context = _fixture_inputs()
    elif len(sys.argv) == 3:
        round_id, source_id = map(int, sys.argv[1:])
        db.init_db()
        round_ = interview_store.get_round(round_id)
        source = next(
            (item for item in interview_store.list_transcript_sources(round_id) if item["id"] == source_id),
            None,
        )
        if not round_ or not source:
            raise SystemExit("round or source not found")
        raw_text = source.get("raw_text") or ""
        segments = interview_store.list_segments(round_id)
        participants = interview_store.list_participants(round_id)
        track = db.get_job_track(round_["track_id"]) or {}
        context = "\n".join(
            item for item in (
                f"公司：{track.get('company')}" if track.get("company") else "",
                f"岗位：{track.get('role')}" if track.get("role") else "",
                f"JD：{track.get('jd')}" if track.get("jd") else "",
            ) if item
        )
    else:
        raise SystemExit("usage: validate_interview_models.py ROUND_ID SOURCE_ID")
    prompt = interview_decompose.ai_exam_prompt(raw_text, context)
    resolved = ai.resolve_model_profile("deep_reasoning")
    primary = resolved["provider"]
    candidates = [primary] + list(primary.get("_fallback_providers") or [])
    results = []
    for candidate in candidates:
        provider = {**candidate, "_fallback_providers": []}
        try:
            response = ai.chat(
                [{"role": "user", "content": prompt}],
                system="严格执行分隔协议。证据不足时留空，绝不把面试官话语写入候选人回答。",
                max_tokens=9000,
                provider=provider,
                timeout=180,
            )
            bundle = interview_decompose.parse_ai_exam_bundle(response, segments, participants)
            quality = interview_decompose.validate_exam_bundle(bundle, raw_text)
            results.append({
                "provider": provider.get("name"),
                "model": provider.get("model"),
                **quality,
                "questions": [item["normalized_question"] for item in bundle["questions"]],
                "role_insight_count": len(bundle["role_insights"]),
            })
        except Exception as exc:
            results.append({
                "provider": provider.get("name"),
                "model": provider.get("model"),
                "accepted": False,
                "error": str(exc),
            })
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
