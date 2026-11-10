import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import Feed, Journal, Pipeline, write_json
from research_appraisal import OpenAI, PRIMARY, BudgetExceeded, GRADE
from research_sources import Drive, Literature, Record, Review, Unavailable, get

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
QUOTE = "The trial reported pain scores for adults allocated to exercise or usual care."


class ProviderErrorTest(unittest.TestCase):
    def test_openai_429_reports_only_known_diagnostic_codes(self):
        url = "https://api.openai.com/v1/responses"
        for code, expected in (("insufficient_quota", "openai_insufficient_quota"),
                               ("credit_balance_exhausted", "openai_credit_balance_exhausted"),
                               ("project_spend_limit_exceeded", "openai_project_spend_limit_exceeded"),
                               ("rate_limit_exceeded", "openai_rate_limit_exceeded"),
                               ("private-provider-text", "http_429")):
            with self.subTest(code=code):
                response = BytesIO(json.dumps({"error": {"code": code}}).encode())
                error = HTTPError(url, 429, "Too Many Requests", {}, response)
                with patch("research_sources.urlopen", side_effect=error) as request:
                    with self.assertRaisesRegex(Unavailable, f"^{expected}$"):
                        get(url, body=b"{}")
                request.assert_called_once()


ARTICLE = "Methods and results. " + (QUOTE + " ") * 40
RECORD = Record("10.1234/example", "12345", "Example exercise trial",
                "Author AB. Example exercise trial. Example Journal.", "PMC12345",
                ARTICLE[:800], True, "randomized controlled trial")
CONFIG = {"query": "test", "lookback_days": 28, "search_limit": 35, "max_candidates": 8}


def reading(level, schema):
    study = {
        "region": "lower limb", "title": "Pain outcomes after exercise",
        "evidence_tag": "emerging", "confidence": "likely", "impact": "know_dont_act",
        "impact_line": "Pain outcomes were reported for exercise and usual care.",
        "summary": ["Adults took part.", "Participants received exercise or usual care.",
                    "The study measured pain."],
        "key_findings": ["Exercise was compared with usual care.", "Pain was measured.",
                         "Clinical importance was uncertain."],
        "limits": ["Applicability beyond the study is unclear."],
        "pico": {"p": "Adults", "i": "Exercise", "c": "Usual care", "o": "Pain"},
        "methods": {"design": "Randomised trial", "participants": "Adults", "intervention": "Exercise",
                    "comparator": "Usual care", "primary_outcome": "Pain"},
        "results": [{"outcome": "Pain", "result": "Uncertain between-group effect", "verdict": "unclear"}],
        "appraisal": {
            "appraised_from": level, "tool": "RoB 2", "certainty": "low",
            "overall_risk_of_bias": "not assessable",
            "domains": [{"domain": domain, "judgement": "unclear", "reason": "Details were unavailable.",
                         "support": "Not reported"} for domain in (
                "Randomisation process", "Deviations from intended interventions", "Missing outcome data",
                "Measurement of the outcome", "Selection of the reported result")],
            "grade": [{"domain": row, "judgement": "not applicable" if row == "inconsistency" else "serious",
                       "reason": "Limited information."}
                      for row in sorted(GRADE)],
            "clinical_significance": ["The meaningful difference is uncertain."],
            "comparator_fairness": ["Usual care dose was unclear."],
            "alternative_explanations": ["Co-intervention details were unclear."],
            "spin": ["The study cannot establish clinical benefit."],
            "fit_with_evidence": ["External evidence was unavailable."],
            "external_validity": ["Transfer to sports practice is unclear."],
            "would_change_my_mind": ["A better controlled trial could clarify effects."],
            "strengths": ["Comparison with usual care."], "weaknesses": ["Limited methods details."],
            "funding_conflicts": "Funding was not reported.",
        },
    }

    def fill(value, subschema):
        if "anyOf" in subschema:
            subschema = next(part for part in subschema["anyOf"]
                             if part.get("type") != "null"
                             and (part.get("type") != "string" or isinstance(value, str))
                             and (part.get("type") != "array" or isinstance(value, list)))
            return fill(value, subschema)
        if isinstance(value, dict):
            return {key: fill(value[key], part) if key in value else None
                    for key, part in subschema["properties"].items()}
        if isinstance(value, list):
            return [fill(item, subschema["items"]) for item in value]
        return value

    return fill({"adequate": True, "reason": "", "card": study,
                 "evidence": dict.fromkeys(("design", "population", "intervention_or_exposure",
                                            "comparator", "outcomes", "results", "numbers"), [QUOTE])}, schema)


class StubLiterature:
    def __init__(self, records, level="full_text"):
        self.records, self.level = records, level
        self.fetches = 0

    def discover(self, *args):
        return self.records

    def identify(self, *args):
        return self.records[0]

    def source(self, record):
        self.fetches += 1
        if self.level == "insufficient":
            raise Review("insufficient_source")
        return self.level, record.url, ARTICLE


class StubDrive:
    def __init__(self, entries=(), marker=None):
        self.files, self.marker = entries, marker

    def entries(self):
        return self.files

    def download(self, entry, limit):
        return json.dumps(self.marker).encode()


class ResearchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for folder in ("schema", "scripts", "papers", "editions"):
            (self.root / folder).mkdir()
        for file in ("schema/edition.schema.json", "APPRAISAL.md", "CARD_STYLE.md",
                     "scripts/validate.py", "scripts/card_style.py"):
            shutil.copy(ROOT / file, self.root / file)
        write_json(self.root / "index.json", {"editions": [], "latest": None})
        write_json(self.root / "papers/index.json", {"papers": []})
        write_json(self.root / "shown.json", {"shown": []})
        self.journal = Journal(self.root / "private-state.json")
        self.run = {"new_holds": 0}
        self.phases = []
        self.response_mode = ""
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-only"}).start()
        patch("research_appraisal.get", side_effect=self.generate).start()

    def generate(self, url, *, headers, body):
        request = json.loads(body)
        phase = request["text"]["format"]["name"]
        payload = json.loads(request["input"])
        schema = request["text"]["format"]["schema"]
        Draft202012Validator.check_schema(schema)
        self.phases.append(phase)
        self.assertFalse(request["store"])
        if phase == "comparison":
            self.assertNotIn("article", payload)
            value = {"agrees": True, "disagreements": []}
        else:
            self.assertEqual({"record", "source_level", "article"}, payload.keys())
            if phase == "blind_verification" and self.response_mode == "failure":
                raise Unavailable("http_429")
            value = reading(payload["source_level"], schema)
            if phase == "blind_verification" and self.response_mode == "disagreement":
                value["card"]["appraisal"]["certainty"] = "very low"
            if self.response_mode == "invented":
                value["card"]["results"][0]["result"] = "Pain fell by 15 points"
        self.assertTrue(Draft202012Validator(schema).is_valid(value))
        return json.dumps({"status": "completed", "model": request["model"],
                           "usage": {"input_tokens": 5000, "output_tokens": 1000},
                           "output": [{"type": "message", "content": [
                               {"type": "output_text", "text": json.dumps(value)}]}]}).encode()

    def pipeline(self, records=(), level="full_text", drive=None, limits=(3_000_000, 15_000_000),
                 run_id="test", journal=None, anchors=None):
        journal = journal or self.journal
        return Pipeline(Feed(self.root), journal, StubLiterature(records, level),
                        drive or StubDrive(), OpenAI(journal, run_id, "2026-11", *limits),
                        NOW, self.run, CONFIG.copy(), anchors)

    def publish(self, pipeline):
        paths = pipeline.feed.publish(pipeline.papers, pipeline.studies, pipeline.today, NOW.isoformat())
        result = subprocess.run([sys.executable, "-X", "utf8", str(self.root / "scripts/validate.py")],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return paths

    def reasons(self, records):
        return {entry["reason"] for entry in records.values()}

    def test_new_full_text_provenance_schema_and_idempotent_retry(self):
        pipeline = self.pipeline([RECORD, RECORD])
        pipeline.process()
        paths = self.publish(pipeline)
        self.assertEqual({"editions/2026-W46.json", "index.json", "shown.json"}, set(paths))
        published = json.loads((self.root / paths[0]).read_text(encoding="utf-8"))
        verification = published["items"][0]["appraisal"]["verification"]
        self.assertEqual("agrees", verification["status"])
        self.assertEqual(RECORD.pmid, published["sources"][0]["pmid"])
        job = next(iter(self.journal.data["jobs"].values()))
        self.assertEqual("full_text", job["source"]["level"])
        self.assertEqual(64, len(job["source"]["text_sha256"]))
        self.assertEqual(3, len(self.journal.data["charges"]))
        self.assertEqual(OpenAI.cost(PRIMARY, 5000, 1000), self.journal.data["charges"][0]["cost"])
        repeat = self.pipeline([RECORD])
        repeat.process()
        self.assertEqual([], self.publish(repeat))
        self.assertEqual(["primary", "blind_verification", "comparison"], self.phases)

    def test_existing_doi_deduplicates_before_fetch_or_payment(self):
        write_json(self.root / "shown.json", {"shown": [{"key": RECORD.doi.upper()}]})
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(0, pipeline.literature.fetches)
        self.assertEqual([], self.phases)

    def test_benchmark_can_review_existing_paper_without_changing_feed(self):
        write_json(self.root / "shown.json", {"shown": [{"key": RECORD.doi.upper()}]})
        before = (self.root / "index.json").read_bytes()
        pipeline = self.pipeline([RECORD])
        pipeline.benchmark(RECORD.pmid)
        self.assertEqual(1, len(pipeline.studies))
        self.assertEqual(["primary", "blind_verification", "comparison"], self.phases)
        self.assertEqual(before, (self.root / "index.json").read_bytes())
        self.assertEqual([], list((self.root / "editions").glob("*.json")))

    def test_model_schema_requires_current_card_format(self):
        schema = self.pipeline().schema
        validator = Draft202012Validator(schema)
        valid = reading("full_text", schema)
        self.assertTrue(validator.is_valid(valid))
        valid["card"]["summary"] = "An older card could use this paragraph."
        self.assertFalse(validator.is_valid(valid))
        valid = reading("full_text", schema)
        valid["card"]["results"][0]["verdict"] = None
        self.assertFalse(validator.is_valid(valid))

    def test_queue_keeps_atlas_id_and_writes_only_existing_feed_format(self):
        drive = StubDrive([{"id": "abc", "name": "queue__00abcdef__exercise.txt"}],
                          {"kind": "queue", "papers": [{"id": "00abcdef", "doi": RECORD.doi}]})
        pipeline = self.pipeline([RECORD], drive=drive)
        pipeline.process()
        self.assertEqual({"papers/00abcdef.json", "papers/index.json"}, set(self.publish(pipeline)))
        self.assertEqual(1, len(drive.files))

    def test_abstract_only_is_checked_then_held_and_cached(self):
        pipeline = self.pipeline([RECORD], "abstract")
        pipeline.process()
        self.assertIn("abstract_only", self.reasons(self.journal.data["holds"]))
        self.assertEqual([], self.publish(pipeline))
        self.pipeline([RECORD], "abstract").process()
        self.assertEqual(3, len(self.phases))

    def test_insufficient_source_and_empty_queue_do_not_call_model(self):
        self.pipeline().process()
        self.assertEqual({}, self.journal.data["holds"])
        pipeline = self.pipeline([RECORD], "insufficient")
        pipeline.process()
        self.assertIn("insufficient_source", self.reasons(self.journal.data["holds"]))
        self.assertEqual([], self.publish(pipeline))
        self.assertEqual([], self.phases)

    def test_independent_disagreement_kept_with_both_readings(self):
        self.response_mode = "disagreement"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual([], self.publish(pipeline))
        job = next(iter(self.journal.data["jobs"].values()))
        self.assertFalse(job["decision"]["agrees"])
        self.assertTrue(job["decision"]["disagreements"])
        self.assertEqual(["primary", "blind_verification"], self.phases)

    def test_unsupported_number_prevents_publication(self):
        self.response_mode = "invented"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertIn("unsupported_number", self.reasons(self.journal.data["holds"]))
        self.assertEqual([], self.publish(pipeline))

    def test_rate_limit_resumes_from_durable_blind_checkpoint(self):
        self.response_mode = "failure"
        with self.assertRaisesRegex(Unavailable, "http_429"):
            self.pipeline([RECORD]).process()
        reloaded = Journal(self.journal.path)
        self.assertEqual("uncertain", reloaded.data["charges"][1]["status"])
        self.response_mode = ""
        pipeline = self.pipeline([RECORD], run_id="retry", journal=reloaded)
        # The failed candidate has moved outside the discovery window.
        with patch.object(pipeline.literature, "discover", return_value=[]):
            pipeline.process()
        self.assertTrue(self.publish(pipeline))
        self.assertEqual(["primary", "blind_verification", "blind_verification", "comparison"], self.phases)

    def test_both_budgets_block_before_generation(self):
        for limits in ((1, 15_000_000), (3_000_000, 1)):
            with self.assertRaises(BudgetExceeded):
                self.pipeline([RECORD], limits=limits).process()
        self.assertEqual([], self.phases)
        self.assertEqual([], self.journal.data["charges"])

    def test_drive_complete_listing_and_failure(self):
        drive = Drive.__new__(Drive)
        drive.folder, drive.headers = "folder", {}
        pages = [{"files": [], "nextPageToken": "next"},
                 {"files": [{"name": "queue__abc.txt", "id": "abc"}]}]
        with patch("research_sources.get", side_effect=[json.dumps(page).encode() for page in pages]):
            self.assertEqual("abc", drive.entries()[0]["id"])
        with patch("research_sources.get", return_value=b'{"incompleteSearch": true}'):
            with self.assertRaisesRegex(Unavailable, "incomplete_drive_listing"):
                drive.entries()

    def test_uploaded_pdf_requires_review_of_exact_bytes_before_model_use(self):
        pdf = {"test": "pdf"}
        drive = StubDrive([{"id": "abc", "name": "00abcdef__Example exercise trial.pdf"}], pdf)
        article = f"{RECORD.title}\n{RECORD.doi}\f{ARTICLE}"
        with patch("research.extract_pdf", return_value=article):
            self.pipeline([RECORD], drive=drive).queue()
            self.assertIn("pdf_external_anchor_review_needed", self.reasons(self.journal.data["holds"]))
            self.assertEqual([], self.phases)
            anchors = {RECORD.doi: {"pdf_sha256": sha256(json.dumps(pdf).encode()).hexdigest(),
                                   "external": [], "reviewed_by": "Reviewer",
                                   "external_searched": "PEDro and review citation search completed."}}
            pipeline = self.pipeline([RECORD], drive=drive, anchors=anchors)
            pipeline.process()
            self.assertIn("papers/00abcdef.json", self.publish(pipeline))

    def test_one_failed_literature_source_does_not_drop_other_candidates(self):
        other = replace(RECORD, doi="10.1234/other", pmid="12346", title="Another trial")
        pipeline = self.pipeline([RECORD, other])
        with patch.object(pipeline.literature, "source", side_effect=[
            Unavailable("http_500"), ("full_text", other.url, ARTICLE),
        ]):
            pipeline.process()
        self.assertIn("http_500", self.reasons(self.run["source_errors"]))
        self.assertEqual(other, pipeline.studies[0][0])
        self.assertTrue(self.publish(pipeline))

    def test_xml_identity_and_retraction_are_checked_before_generation(self):
        xml = (f'<article><front><article-meta><article-id pub-id-type="pmid">12345</article-id>'
               f'</article-meta></front><body><sec>{ARTICLE}</sec></body></article>').encode()
        with patch("research_sources.get", return_value=xml):
            self.assertEqual("full_text", Literature().source(RECORD)[0])
        with patch("research_sources.get", return_value=xml.replace(b">12345<", b">54321<")):
            with self.assertRaisesRegex(Review, "full_text_identity_mismatch"):
                Literature().source(RECORD)
        hit = {"title": RECORD.title, "pmid": RECORD.pmid,
               "commentCorrectionList": {"commentCorrection": [{"type": "Retraction in"}]}}
        self.assertTrue(Record.from_result(hit).excluded)


if __name__ == "__main__":
    unittest.main()
