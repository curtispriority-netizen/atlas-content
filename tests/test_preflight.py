import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.preflight import daily_work, weekly_work


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")

    def test_empty_or_unrelated_queue_does_not_read_repo_data(self):
        self.assertFalse(daily_work(self.root, []))
        self.assertFalse(daily_work(self.root, ["Notes.txt", "paper.pdf"]))

    def test_incomplete_listing_shape_cannot_return_no_work(self):
        with self.assertRaises(ValueError):
            daily_work(self.root, {"files": []})

    def test_all_marker_types_leave_work_for_the_worker(self):
        for name in ("queue__batch__123.txt", "question__123__Title.txt", "delete__abcdef12.txt", "queue__"):
            with self.subTest(name=name):
                self.assertTrue(daily_work(self.root, [name]))

    def test_new_pdf_is_work(self):
        self.write("papers/index.json", {"papers": []})
        self.assertTrue(daily_work(self.root, ["abcdef12__Achilles.pdf"]))

    def test_pdf_is_skipped_only_with_a_checked_full_text_format_two_card(self):
        paper_id = "abcdef12"
        path = f"papers/{paper_id}.json"
        names = [f"{paper_id}__Achilles.PDF"]
        self.write("papers/index.json", {"papers": [{"id": paper_id, "path": path}]})
        self.assertTrue(daily_work(self.root, names))
        verification = {
            "status": "disagreements",
            "checked_by": "independent second pass (full text)",
            "checked_at": "2026-11-10T00:00:00Z",
        }
        study = {
            "item_id": f"mine-{paper_id}",
            "format": 2,
            "read_level": "full_text",
            "appraisal": {"appraised_from": "full_text", "verification": verification},
        }
        paper = {"paper_id": paper_id, "study": study}
        self.write(path, paper)
        self.assertFalse(daily_work(self.root, names))

        for obj, key, value in (
            (verification, "status", "not checked"),
            (verification, "checked_at", ""),
            (study, "format", 1),
            (study, "read_level", "abstract"),
        ):
            with self.subTest(key=key):
                original = obj[key]
                obj[key] = value
                self.write(path, paper)
                self.assertTrue(daily_work(self.root, names))
                obj[key] = original

    def test_weekly_uses_iso_week_year_and_checks_index(self):
        hobart_date = date(2027, 1, 1)
        self.write("index.json", {"editions": []})
        self.assertTrue(weekly_work(self.root, hobart_date))
        path = "editions/2026-W53.json"
        self.write(path, {"edition_id": "2026-W53"})
        with self.assertRaisesRegex(ValueError, "file and index disagree"):
            weekly_work(self.root, hobart_date)
        self.write("index.json", {"editions": [{"id": "2026-W53", "path": path}]})
        self.assertFalse(weekly_work(self.root, hobart_date))
