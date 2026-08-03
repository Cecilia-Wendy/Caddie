"""Deterministic delivery diagnostics for interview answers."""
from __future__ import annotations

import re
from pathlib import Path


class SpeechError(RuntimeError):
    pass


def transcribe_audio(path: Path, mime_type: str | None = None, prompt: str = "") -> dict:
    """Keep the legacy API explicit while Caddie uses system input methods."""
    raise SpeechError(
        "Caddie 当前不内置语音转写；请在答案框中使用微信输入法的语音快捷键，或直接输入文字。"
    )


FILLER_GROUPS = {
    "犹豫词": ("嗯", "呃", "额", "啊", "哦", "呐"),
    "口头衔接": ("然后", "就是", "那个", "其实", "我觉得", "对吧", "怎么说", "可能"),
}

STRUCTURE_MARKERS = (
    "第一", "第二", "第三", "首先", "其次", "最后", "一是", "二是", "三是",
    "因此", "所以", "结论", "具体来说", "总的来说",
)


def _count_term(text: str, term: str) -> int:
    return len(re.findall(re.escape(term), text, flags=re.IGNORECASE))


def clean_transcript(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"(^|[\s，。！？；：])[嗯呃额啊哦呐]+(?=[\s，。！？；：]|$)", r"\1", cleaned)
    cleaned = re.sub(r"(然后|就是|那个)(?:[\s，]*)\1", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s*([，。！？；：])\s*", r"\1", cleaned)
    return cleaned.strip(" ，")


def analyze_delivery(text: str, duration_ms: int | None = None) -> dict:
    """Analyze observable wording habits without inventing audio-level signals."""
    raw = str(text or "").strip()
    counts = []
    group_totals = {}
    for group, terms in FILLER_GROUPS.items():
        total = 0
        for term in terms:
            count = _count_term(raw, term)
            total += count
            if count:
                counts.append({"term": term, "count": count, "group": group})
        group_totals[group] = total
    counts.sort(key=lambda item: (-item["count"], item["term"]))

    char_count = len(re.sub(r"\s|[，。！？；：,.!?;:]", "", raw))
    sentences = [item.strip() for item in re.split(r"[。！？!?；;\n]+", raw) if item.strip()]
    sentence_lengths = [len(re.sub(r"\s|[，：,:]", "", item)) for item in sentences]
    max_sentence_chars = max(sentence_lengths, default=0)
    long_sentence_count = sum(1 for length in sentence_lengths if length >= 80)
    marker_counts = [
        {"term": marker, "count": _count_term(raw, marker)}
        for marker in STRUCTURE_MARKERS
        if _count_term(raw, marker)
    ]
    structure_marker_total = sum(item["count"] for item in marker_counts)

    duration_seconds = max(0.0, float(duration_ms or 0) / 1000)
    chars_per_minute = round(char_count / duration_seconds * 60) if duration_seconds > 1 else None
    filler_total = sum(group_totals.values())
    density = round(filler_total / max(char_count, 1) * 100, 1)
    if chars_per_minute is None:
        pace = "未知"
    elif chars_per_minute < 170:
        pace = "偏慢"
    elif chars_per_minute > 330:
        pace = "偏快"
    else:
        pace = "适中"

    if char_count >= 600:
        verbosity_level = "偏长"
    elif char_count >= 350:
        verbosity_level = "较长"
    else:
        verbosity_level = "适中"

    suggestions = []
    if filler_total >= 4 or density >= 2:
        suggestions.append("先停顿再开口，用完整句替代语气词和重复衔接")
    if char_count >= 120 and structure_marker_total < 2:
        suggestions.append("先给结论，再用‘第一、第二、因此’标记答案结构")
    if long_sentence_count:
        suggestions.append("把超过 80 字的长句拆成判断、证据和结果三句")
    if verbosity_level in {"较长", "偏长"}:
        suggestions.append("删除不推动结论的背景和重复解释，优先保留事实与取舍")
    if pace == "偏快":
        suggestions.append("数据、结论和转折处主动停顿，给面试官消化时间")
    elif pace == "偏慢":
        suggestions.append("先给结论再展开证据，减少边想边说")
    if not suggestions:
        suggestions.append("表达结构较稳定，继续保持先结论、后证据、再回扣岗位")

    return {
        "duration_ms": int(duration_ms or 0),
        "char_count": char_count,
        "sentence_count": len(sentences),
        "max_sentence_chars": max_sentence_chars,
        "long_sentence_count": long_sentence_count,
        "chars_per_minute": chars_per_minute,
        "pace": pace,
        "verbosity_level": verbosity_level,
        "filler_total": filler_total,
        "filler_density": density,
        "group_totals": group_totals,
        "top_fillers": counts[:6],
        "structure_marker_total": structure_marker_total,
        "structure_markers": marker_counts,
        "suggestions": suggestions,
        "limitation": (
            "文字可以分析保留下来的语气词、重复、结构、长句与冗余；无法判断真实语速、停顿、"
            "音量和情绪。若输入法开启自动润色，口头词统计可能偏低。"
        ),
    }
