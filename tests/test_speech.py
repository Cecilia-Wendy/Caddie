"""Deterministic checks for interview response cleanup and delivery analysis."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import speech


def main():
    try:
        speech.transcribe_audio(Path("answer.wav"), prompt="AI 产品经理面试")
    except speech.SpeechError as exc:
        assert "微信输入法" in str(exc), exc
    else:
        raise AssertionError("Caddie 不应再内置收费或本地语音转写")

    raw = "嗯，我觉得那个，就是这个项目，然后然后我们先看用户价值，然后再看成本。"
    cleaned = speech.clean_transcript(raw)
    assert not cleaned.startswith("嗯"), cleaned
    assert "然后然后" not in cleaned, cleaned
    assert "用户价值" in cleaned, cleaned

    metrics = speech.analyze_delivery(raw)
    assert metrics["filler_total"] >= 5, metrics
    assert metrics["chars_per_minute"] is None, metrics
    assert metrics["pace"] == "未知", metrics
    assert metrics["top_fillers"][0]["count"] >= 1, metrics
    assert "无法判断真实语速" in metrics["limitation"], metrics
    assert metrics["suggestions"], metrics

    structured = speech.analyze_delivery(
        "第一，先说明用户问题。第二，再解释方案边界。最后，给出验证方法。"
    )
    assert structured["structure_marker_total"] >= 3, structured

    timed = speech.analyze_delivery("这是一个需要说明结构和证据的完整回答。", duration_ms=10_000)
    assert timed["chars_per_minute"] is not None, timed
    assert timed["pace"] in {"偏慢", "适中", "偏快"}, timed
    print("SPEECH_OK", cleaned, metrics["filler_total"], structured["structure_marker_total"])


if __name__ == "__main__":
    main()
