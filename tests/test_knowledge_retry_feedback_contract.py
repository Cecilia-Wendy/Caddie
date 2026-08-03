"""Retry actions must provide immediate, visible progress feedback."""
from pathlib import Path


source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")

assert "STATE.knowledgeRetryingIds=targets;renderKnowledgeCoachCandidate()" in source
assert "已开始重试 ${targets.length} 篇文档" in source
assert "正在重新生成…" in source
assert "正在重试 ${retrying.length} 篇…" in source
assert "STATE.knowledgeRetryingIds=[]" in source

print("KNOWLEDGE_RETRY_FEEDBACK_CONTRACT_OK")
