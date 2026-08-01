import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class ExperienceEditContractTests(unittest.TestCase):
    def test_detail_header_exposes_edit_entry(self):
        self.assertIn('onclick="editExperience(${exp.id})"', HTML)
        self.assertIn('title="编辑经历信息"', HTML)

    def test_editor_supports_all_persisted_experience_fields(self):
        start = HTML.index("async function editExperience")
        end = HTML.index("function addProject", start)
        editor = HTML[start:end]
        for field in ("company", "role", "start_date", "end_date", "location"):
            self.assertIn("k:'" + field + "'", editor)
        self.assertIn("method:'PUT'", editor)
        self.assertIn("'/experiences/'+id", editor)

    def test_dates_use_constrained_date_inputs(self):
        self.assertIn("k:'start_date',label:'开始日期',type:'date'", HTML)
        self.assertIn("k:'end_date',label:'结束日期（留空表示至今）',type:'date'", HTML)
        self.assertIn("function experienceFormDate", HTML)


if __name__ == "__main__":
    unittest.main()
