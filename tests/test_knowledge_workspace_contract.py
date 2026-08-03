from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "caddie-ui.css").read_text(encoding="utf-8")


class KnowledgeWorkspaceContractTests(unittest.TestCase):
    def test_library_uses_one_toolbar_control_per_side(self):
        workspace = HTML.split("function renderKnowledgeWorkspace(items){", 1)[1].split(
            "function knowledgePanelControl", 1
        )[0]
        self.assertNotIn("knowledge-edge-control", workspace)
        self.assertEqual(workspace.count("knowledgePanelControl('nav')"), 1)
        self.assertEqual(workspace.count("knowledgePanelControl('outline')"), 1)

    def test_library_and_track_documents_share_fullscreen_control(self):
        workspace = HTML.split("function renderKnowledgeWorkspace(items){", 1)[1].split(
            "function knowledgePanelControl", 1
        )[0]
        track = HTML.split("function renderTrackKnowledgeDocument", 1)[1].split(
            "function openTrackKnowledgeDoc", 1
        )[0]
        self.assertIn("knowledgeFocusControl()", workspace)
        self.assertIn("knowledgeFocusControl()", track)
        self.assertIn(".knowledge-library-workspace.focus-expanded", CSS)
        self.assertIn(".track-knowledge-workspace,.knowledge-library-workspace", HTML)

    def test_both_entries_render_the_same_knowledge_document_component(self):
        workspace = HTML.split("function renderKnowledgeWorkspace(items){", 1)[1].split(
            "function knowledgePanelControl", 1
        )[0]
        track = HTML.split("function renderTrackKnowledgeDocument", 1)[1].split(
            "function openTrackKnowledgeDoc", 1
        )[0]
        self.assertIn("knowledgeDocument(selected)", workspace)
        self.assertIn("knowledgeDocument(doc)", track)
        self.assertIn("STATE.knowledgeOpen=kid", HTML)


if __name__ == "__main__":
    unittest.main()
