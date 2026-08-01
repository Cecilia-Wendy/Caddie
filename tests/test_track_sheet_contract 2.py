from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class TrackSheetContractTests(unittest.TestCase):
    def test_readiness_is_not_a_visible_or_sortable_track_field(self):
        field_block = HTML.split("const TRACK_FIELD_BASE=[", 1)[1].split("];", 1)[0]
        sort_block = HTML.split("function openTrackSortPanel(){", 1)[1].split("\n}", 1)[0]
        cell_block = HTML.split("function trackFieldCell(key,t){", 1)[1].split(
            "function renderTrackSheet", 1
        )[0]
        self.assertNotIn("readiness", field_block)
        self.assertNotIn("准备度", field_block)
        self.assertNotIn("准备度", sort_block)
        self.assertNotIn("readiness:", cell_block)

    def test_old_saved_readiness_sort_falls_back_to_focus(self):
        filters = HTML.split("function getTrackFilters(){", 1)[1].split("\n}", 1)[0]
        self.assertIn("readiness_asc", filters)
        self.assertIn("STATE.trackFilters.sort='focus'", filters)


if __name__ == "__main__":
    unittest.main()
