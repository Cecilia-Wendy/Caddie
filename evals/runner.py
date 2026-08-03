"""Command-line runner for Caddie's evaluation suite."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.graders import grade_case


DEFAULT_CASES = Path(__file__).with_name("cases") / "core.jsonl"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    items = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return items


def response_map(path: Path) -> dict[str, str]:
    rows = load_jsonl(path)
    return {str(row["case_id"]): str(row.get("output") or "") for row in rows}


def _prompt(case: dict[str, Any]) -> str:
    return (
        f"任务：{case['task']}\n\n"
        f"目标岗位：{case.get('job_description') or '未指定'}\n\n"
        f"用户确认的材料：\n{case.get('source_material') or '（无）'}\n\n"
        "请直接交付可供用户审阅的成品。只能使用材料中的事实和数字；信息不足时明确标注需要用户补充，禁止自行编造。"
    )


def generate_live(case: dict[str, Any], profile_key: str) -> tuple[str, dict[str, Any]]:
    import ai

    started = time.monotonic()
    output = ai.chat(
        [{"role": "user", "content": _prompt(case)}],
        system="你是 Caddie 的求职内容助手。真实、具体、可追溯高于辞藻华丽。",
        max_tokens=1800,
        timeout=120,
        profile_key=profile_key,
    )
    info = ai.last_call_info() or {}
    info["wall_time_ms"] = int((time.monotonic() - started) * 1000)
    return output, info


def run(cases: list[dict[str, Any]], outputs: dict[str, str] | None = None,
        *, live: bool = False, profile_key: str = "deep_reasoning") -> dict[str, Any]:
    results = []
    for case in cases:
        metadata: dict[str, Any] = {}
        if live:
            output, metadata = generate_live(case, profile_key)
        elif outputs is not None:
            output = outputs.get(str(case["id"]), "")
        else:
            output = str(case.get("smoke_output") or "")
        result = grade_case(case, output)
        result["output"] = output
        result["run"] = metadata
        results.append(result)

    passed = sum(1 for item in results if item["passed"])
    scores = [item["score"] for item in results]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else ("responses" if outputs is not None else "smoke"),
        "summary": {
            "cases": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4) if results else 0.0,
            "average_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        },
        "results": results,
    }


def render_console(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"CADDIE_EVAL mode={report['mode']} cases={summary['cases']} pass_rate={summary['pass_rate']:.1%} avg={summary['average_score']:.1%}")
    for result in report["results"]:
        state = "PASS" if result["passed"] else "FAIL"
        failures = ",".join(result["hard_failures"]) or "-"
        print(f"{state} {result['case_id']} score={result['score']:.1%} hard_failures={failures}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Caddie's local eval suite")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--responses", type=Path, help="JSONL rows: {case_id, output}")
    parser.add_argument("--live", action="store_true", help="Run the configured Caddie AI provider")
    parser.add_argument("--profile", default="deep_reasoning", help="Caddie model profile for --live")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report here")
    parser.add_argument("--fail-on-regression", action="store_true", help="Exit 1 when any case fails")
    args = parser.parse_args(argv)
    if args.live and args.responses:
        parser.error("--live and --responses are mutually exclusive")

    cases = load_jsonl(args.cases)
    outputs = response_map(args.responses) if args.responses else None
    report = run(cases, outputs, live=args.live, profile_key=args.profile)
    render_console(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"REPORT {args.output}")
    return 1 if args.fail_on_regression and report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
