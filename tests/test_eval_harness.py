"""Regression coverage for the local Caddie eval harness."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.graders import grade_case
from evals.runner import DEFAULT_CASES, load_jsonl, run


def main():
    cases = load_jsonl(DEFAULT_CASES)
    assert len(cases) >= 4

    smoke = run(cases)
    assert smoke["summary"]["pass_rate"] == 1.0, smoke
    assert smoke["summary"]["average_score"] >= 0.9, smoke

    case = cases[0]
    fabricated = "我独立负责整个项目，访谈 12 名用户，使收入增长 50%。"
    result = grade_case(case, fabricated)
    assert not result["passed"]
    assert "forbidden_claims" in result["hard_failures"]
    assert "numeric_grounding" in result["hard_failures"]
    assert "uncertainty_boundary" in result["hard_failures"]

    missing = run(cases, outputs={})
    assert missing["summary"]["passed"] == 0
    print("EVAL_HARNESS_OK", smoke["summary"])


if __name__ == "__main__":
    main()
