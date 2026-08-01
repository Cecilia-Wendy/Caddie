"""A document confirmation stays bound to its task and shows immediate progress."""
from pathlib import Path


source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")

assert "applyKnowledgeCoachCandidates([${x.id}],${task.id})" in source
assert "async function applyKnowledgeCoachCandidates(selectedIds=[],expectedTaskId=null)" in source
assert "STATE.knowledgeApplyingIds=ids;renderKnowledgeCoachCandidate()" in source
assert "正在确认并写入这篇文档…" in source
assert "正在写入知识库…" in source
assert "这篇已经写入，无需重复确认" in source
assert "这篇尚未生成完成，请先重试" in source

print("KNOWLEDGE_CONFIRM_FEEDBACK_CONTRACT_OK")
