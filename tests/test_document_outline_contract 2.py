"""Static regression contract for document outlines."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def main():
    # All Markdown-backed document surfaces share one outline renderer.
    assert INDEX.count("knowledgeProjectOutline(") >= 6
    assert "function knowledgeOutline(" not in INDEX
    assert "knowledge-outline-row level-" not in INDEX

    # Every live knowledge document keeps the active outline in sync.
    assert INDEX.count('id="knowledgeDocument" onscroll="updateKnowledgeOutlineActive()"') >= 4

    # Read-only assets expose real rendered headings; edit mode must not offer
    # links that cannot be resolved.
    assert 'class="knowledge-document-body" id="knowledgeReadBody"' in INDEX
    assert "保存正文后，可使用章节目录定位。" in INDEX

    # Outline auto-follow must never scroll the outer page.
    knowledge_sync = INDEX.split("function updateKnowledgeOutlineActive(){", 1)[1].split(
        "\n}", 1
    )[0]
    project_sync = INDEX.split("function updateProjectOutlineActive(){", 1)[1].split(
        "\n}", 1
    )[0]
    assert "scrollIntoView" not in knowledge_sync
    assert "scrollIntoView" not in project_sync

    print("DOCUMENT_OUTLINE_CONTRACT_OK")


if __name__ == "__main__":
    main()
