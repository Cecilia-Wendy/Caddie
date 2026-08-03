"""Knowledge generation must not present a client polling timeout as task failure."""
from pathlib import Path


source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")

assert "if(['queued','active','running'].includes(task.status))" in source
assert "monitorKnowledgeCoachTask(created.id,pending,STATE.session)" in source
assert "文档较长，已转为后台逐篇生成" in source
assert "currentTaskId===Number(taskId)" in source
assert "(!currentTaskId||currentTaskId===Number(taskId))" in source
assert "if(task.status==='review'||task.status==='completed')" in source

print("KNOWLEDGE_BACKGROUND_POLL_CONTRACT_OK")
