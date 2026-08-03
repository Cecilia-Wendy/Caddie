import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "caddie-ui.css").read_text(encoding="utf-8")


class TrackOverviewContractTests(unittest.TestCase):
    def test_overview_is_the_default_workbench_tab(self):
        self.assertIn("let tab=STATE.tracksTab||'overview'", HTML)
        self.assertIn("setTracksTab('overview')\">首页", HTML)
        self.assertIn("if(v==='tracks'&&STATE.view!=='tracks')", HTML)

    def test_overview_reuses_application_records(self):
        self.assertIn("function renderTrackOverview(tracks)", HTML)
        self.assertIn("trackStageValue(t)", HTML)
        self.assertIn("dateValue(t.applied_date)", HTML)
        self.assertIn("dateValue(t.next_interview_at)", HTML)

    def test_summary_has_decision_sections_and_drill_down(self):
        for label in ("当前进展", "流程分布", "下一步", "最近投递"):
            self.assertIn(label, HTML)
        self.assertIn("function openTrackOverviewStage", HTML)
        self.assertIn("STATE.tracksTab='targets'", HTML)

    def test_overview_does_not_reintroduce_readiness(self):
        start = HTML.index("function renderTrackOverview")
        end = HTML.index("function setTrackDensity", start)
        self.assertNotIn("准备度", HTML[start:end])
        self.assertNotIn("readiness", HTML[start:end].lower())

    def test_overview_has_responsive_visual_contract(self):
        self.assertIn(".track-overview-home", CSS)
        self.assertIn(".track-pipeline-strip", CSS)
        self.assertIn(".track-pipeline-legend", CSS)
        self.assertIn("@media(max-width:700px)", CSS)

    def test_pipeline_uses_caddie_semantic_palette(self):
        start = HTML.index("const stageColors=")
        palette = HTML[start:HTML.index(";", start)]
        self.assertNotIn("#6f95ed", palette)
        self.assertNotIn("#b06bb1", palette)
        self.assertIn("interview:'#3370FF'", palette)
        self.assertIn("offer:'#22A273'", palette)


if __name__ == "__main__":
    unittest.main()
