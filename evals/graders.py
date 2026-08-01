"""Deterministic graders for Caddie career-content evaluations.

The graders intentionally favour explainable checks over a single opaque score.
They can run without an AI provider and are safe to use in CI.
"""
from __future__ import annotations

import re
from typing import Any


NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*(?:%|％|万|亿|千|年|月|人|家|项|次|倍)?")
UNCERTAINTY_MARKERS = ("需要补充", "待补充", "请确认", "信息不足", "未提供", "无法确认", "（假设）", "(假设)")
GENERIC_PHRASES = ("贵公司", "行业领先", "充满热情", "高度匹配", "宝贵机会", "贡献自己的力量")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _number_tokens(value: str) -> set[str]:
    return {_compact(item) for item in NUMBER_RE.findall(value or "")}


def _contains_any(text: str, alternatives: list[str] | tuple[str, ...]) -> bool:
    compact = _compact(text)
    return any(_compact(item) in compact for item in alternatives if item)


def _check(name: str, passed: bool, score: float, detail: str, *, hard: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "score": round(score if passed else 0.0, 4),
        "max_score": round(score, 4),
        "hard": hard,
        "detail": detail,
    }


def grade_case(case: dict[str, Any], output: str) -> dict[str, Any]:
    """Grade one output using the transparent contract stored in an eval case."""
    expected = case.get("expected") or {}
    checks: list[dict[str, Any]] = []
    output = str(output or "").strip()

    checks.append(_check("non_empty", bool(output), 1.0, "输出不能为空", hard=True))

    forbidden = [str(x) for x in expected.get("forbidden_claims") or []]
    hits = [item for item in forbidden if _contains_any(output, [item])]
    checks.append(_check(
        "forbidden_claims", not hits, 3.0,
        "不得出现未获支持的事实" + (f"；命中：{hits}" if hits else ""), hard=True,
    ))

    supported_numbers = _number_tokens(" ".join(str(x) for x in expected.get("supported_numbers") or []))
    output_numbers = _number_tokens(output)
    unsupported_numbers = sorted(output_numbers - supported_numbers)
    checks.append(_check(
        "numeric_grounding", not unsupported_numbers, 3.0,
        "输出中的数字必须来自材料" + (f"；无依据数字：{unsupported_numbers}" if unsupported_numbers else ""),
        hard=True,
    ))

    fact_groups = expected.get("required_fact_groups") or []
    covered = [group for group in fact_groups if _contains_any(output, [str(x) for x in group])]
    fact_ratio = len(covered) / len(fact_groups) if fact_groups else 1.0
    checks.append(_check(
        "fact_coverage", fact_ratio >= float(expected.get("min_fact_coverage", 0.67)), 2.0,
        f"关键事实覆盖 {len(covered)}/{len(fact_groups)}",
    ))

    relevance_groups = expected.get("relevance_groups") or []
    relevant = [group for group in relevance_groups if _contains_any(output, [str(x) for x in group])]
    relevance_ratio = len(relevant) / len(relevance_groups) if relevance_groups else 1.0
    checks.append(_check(
        "job_relevance", relevance_ratio >= float(expected.get("min_relevance_coverage", 0.5)), 2.0,
        f"岗位关键词覆盖 {len(relevant)}/{len(relevance_groups)}",
    ))

    missing_information = bool(case.get("missing_information"))
    boundary_ok = (not missing_information) or _contains_any(output, UNCERTAINTY_MARKERS)
    checks.append(_check(
        "uncertainty_boundary", boundary_ok, 2.0,
        "资料不足时必须明确标注待补充或待确认", hard=missing_information,
    ))

    generic_hits = [phrase for phrase in GENERIC_PHRASES if phrase in output]
    max_generic = int(expected.get("max_generic_phrases", 1))
    checks.append(_check(
        "specificity", len(generic_hits) <= max_generic, 1.0,
        f"套话命中 {len(generic_hits)} 处" + (f"：{generic_hits}" if generic_hits else ""),
    ))

    total = sum(item["score"] for item in checks)
    maximum = sum(item["max_score"] for item in checks)
    hard_failures = [item["name"] for item in checks if item["hard"] and not item["passed"]]
    threshold = float(case.get("pass_threshold", 0.75))
    normalized = total / maximum if maximum else 0.0
    return {
        "case_id": case.get("id"),
        "name": case.get("name"),
        "passed": normalized >= threshold and not hard_failures,
        "score": round(normalized, 4),
        "threshold": threshold,
        "hard_failures": hard_failures,
        "checks": checks,
    }
