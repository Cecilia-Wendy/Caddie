import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "caddie-ui.css").read_text(encoding="utf-8")


class TrackRefreshContractTests(unittest.TestCase):
    def test_table_header_has_refresh_action(self):
        self.assertIn('onclick="refreshTrackTable(this)"', HTML)
        self.assertIn('刷新表格', HTML)

    def test_refresh_reloads_without_resetting_filters(self):
        start = HTML.index("async function refreshTrackTable")
        end = HTML.index("function openTrackOverviewStage", start)
        block = HTML[start:end]
        self.assertIn("api('/job-tracks')", block)
        self.assertIn("renderTrackTableBody(tracks)", block)
        self.assertNotIn("trackFilters=", block)

    def test_refresh_has_busy_feedback(self):
        self.assertIn("is-refreshing", HTML)
        self.assertIn("track-refresh-spin", CSS)


if __name__ == "__main__":
    unittest.main()
