import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import Feed, Journal, Pipeline, approved, main, write_json
from research_appraisal import (OpenAI, PRIMARY, BudgetExceeded, GRADE, appraisal_checks, drop_null,
                                excerpt_diagnostics, passage_id, ref_sample, strip_refs)
from research_sources import (Drive, Literature, Record, Review, Unavailable, get, passages,
                              render_passages)
from research_review import render_review
from revalidate_research import revalidate

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
QUOTE = "The trial reported pain scores for adults allocated to exercise or usual care."
SOURCE_CONTEXT = "The intervention group attended exercise sessions."
NUMBER_LINE = "Pain fell by 12 points in the exercise group."
BAD_ID = "P9999"
GOOD, NUMBERS = "P2", "P4"


class StagedPipeline(Pipeline):
    """Test view: the cards a reviewer could approve right now, in the shape the feed publishes."""

    @property
    def papers(self):
        return approved(self.journal, self.staged)[0]

    @property
    def studies(self):
        return approved(self.journal, self.staged)[1]


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


ARTICLE = "Methods and results.\n" + "\n".join([QUOTE, SOURCE_CONTEXT, NUMBER_LINE] * 20)
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
        "results": [{"outcome": "Pain", "result": "Uncertain between-group effect", "verdict": "unclear",
                     "evidence_refs": [GOOD]}],
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
                                            "comparator", "outcomes", "results", "numbers"), [GOOD])}, schema)


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
        self.repair_payloads = []
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
        if phase == "repair":
            self.assertEqual({"record", "source_level", "article", "draft", "violations"}, payload.keys())
            self.assertTrue(payload["violations"])
            self.repair_payloads.append(payload)
            value = reading(payload["source_level"], schema)
            # These modes model a repair that does not fix the defect. Others return a clean reading.
            if self.response_mode == "invented":
                value["card"]["results"][0]["result"] = "Pain fell by 15 points"
            if self.response_mode in ("style_unrepairable", "style_and_excerpt"):
                value["card"]["bottom_line"] = " ".join(["word"] * 45)
            if self.response_mode in ("primary", "style_and_excerpt"):
                value["evidence"]["results"] = [BAD_ID]
        else:
            self.assertEqual({"record", "source_level", "article"}, payload.keys())
            self.assertTrue(payload["article"].startswith("[P1] "))
            self.assertIn(f"] {QUOTE}", payload["article"])
            if self.response_mode == "failure":
                raise Unavailable("http_429")
            value = reading(payload["source_level"], schema)
            if self.response_mode == "invented":
                value["card"]["results"][0]["result"] = "Pain fell by 15 points"
            if self.response_mode == phase:
                value["evidence"]["results"] = [BAD_ID]
            if phase == "primary" and self.response_mode in ("style_only", "style_unrepairable"):
                value["card"]["bottom_line"] = " ".join(["word"] * 45)
            if self.response_mode == "style_and_excerpt":
                value["card"]["bottom_line"] = " ".join(["word"] * 45)
                value["evidence"]["results"] = [BAD_ID]
            if phase == "primary" and self.response_mode == "excerpt_and_number":
                value["card"]["results"][0]["result"] = "Pain fell by 15 points"
                value["evidence"]["results"] = [BAD_ID]
            if phase == "primary" and self.response_mode == "missing_domain":
                value["card"]["appraisal"]["domains"].pop()
        self.assertTrue(Draft202012Validator(schema).is_valid(value))
        if self.response_mode == "invalid_json":
            return json.dumps({"status": "completed", "model": request["model"],
                               "usage": {"input_tokens": 5000, "output_tokens": 1000},
                               "output": [{"type": "message", "content": [
                                   {"type": "output_text", "text": "{private-invalid-json"}]}]}).encode()
        return json.dumps({"status": "completed", "model": request["model"],
                           "usage": {"input_tokens": 5000, "output_tokens": 1000},
                           "instructions": "provider-envelope-only",
                           "output": [{"type": "message", "content": [
                               {"type": "output_text", "text": json.dumps(value)}]}]}).encode()

    def pipeline(self, records=(), level="full_text", drive=None, limits=(3_000_000, 15_000_000),
                 run_id="test", journal=None, anchors=None):
        journal = journal or self.journal
        return StagedPipeline(Feed(self.root), journal, StubLiterature(records, level),
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
        self.assertEqual("not checked", verification["status"])
        self.assertIn("approved by a clinician", verification["checked_by"])
        self.assertEqual(RECORD.pmid, published["sources"][0]["pmid"])
        job = next(iter(self.journal.data["jobs"].values()))
        self.assertEqual("full_text", job["source"]["level"])
        self.assertEqual(64, len(job["source"]["text_sha256"]))
        self.assertEqual(1, len(self.journal.data["charges"]))
        self.assertEqual(OpenAI.cost(PRIMARY, 5000, 1000), self.journal.data["charges"][0]["cost"])
        repeat = self.pipeline([RECORD])
        repeat.process()
        self.assertEqual([], self.publish(repeat))
        self.assertEqual(["primary"], self.phases)

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
        self.assertEqual(["primary"], self.phases)
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
        valid = reading("full_text", schema)
        valid["card"]["appraisal"]["tool"] = "An unrecognised bias tool"
        self.assertFalse(validator.is_valid(valid))

    def test_queue_keeps_atlas_id_and_writes_only_existing_feed_format(self):
        drive = StubDrive([{"id": "abc", "name": "queue__00abcdef__exercise.txt"}],
                          {"kind": "queue", "papers": [{"id": "00abcdef", "doi": RECORD.doi}]})
        pipeline = self.pipeline([RECORD], drive=drive)
        pipeline.process()
        self.assertEqual({"papers/00abcdef.json", "papers/index.json"}, set(self.publish(pipeline)))
        self.assertEqual(1, len(drive.files))

    def test_abstract_only_is_held_without_paying_for_a_model_call(self):
        pipeline = self.pipeline([RECORD], "abstract")
        pipeline.process()
        self.assertIn("abstract_only", self.reasons(self.journal.data["holds"]))
        self.assertEqual([], self.publish(pipeline))
        self.pipeline([RECORD], "abstract").process()
        self.assertEqual([], self.phases)
        self.assertEqual([], self.journal.data["charges"])

    def test_all_failing_checks_are_reported_with_the_first_reason_kept(self):
        schema = self.pipeline().schema
        result = drop_null(reading("full_text", schema))
        result["card"]["bottom_line"] = " ".join(["word"] * 45)
        result["evidence"]["results"] = [BAD_ID]
        with self.assertRaisesRegex(Review, "^card_style_validation_failed$") as raised:
            appraisal_checks(result, ARTICLE, "full_text")
        self.assertEqual(["bottom_line"], raised.exception.fields)
        self.assertEqual(["card_style_validation_failed", "unknown_passage_id"],
                         [row["reason"] for row in raised.exception.problems])
        self.assertEqual(["evidence.results[0]"], raised.exception.problems[1]["fields"])

    def test_every_failure_is_retained_privately_after_one_paid_response(self):
        self.response_mode = "style_and_excerpt"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary", "repair"], self.phases)
        job = next(iter(Journal(self.journal.path).data["jobs"].values()))
        failure = job["validation_failures"]["primary"]
        self.assertEqual("card_style_validation_failed", failure["reason"])
        self.assertIn("original_reading", failure)
        bad = next(row for row in failure["excerpts"] if not row["known"])
        self.assertEqual("evidence.results[0]", bad["field"])
        self.assertEqual(["card_style_validation_failed", "unknown_passage_id"],
                         [row["reason"] for row in failure["all_problems"]])
        report = revalidate(job, ARTICLE)["primary"]
        self.assertEqual("held", report["status"])
        self.assertEqual(2, len(report["all_problems"]))

    def test_instructions_tell_the_model_to_cite_passage_ids_not_copy_text(self):
        from research_appraisal import REPAIR, SYSTEM
        for text in (SYSTEM, REPAIR):
            self.assertIn("passage ID", text)
            self.assertIn("evidence_refs", text)
            self.assertNotIn("verbatim", text)
            self.assertNotIn("quotation marks", text)
        self.assertIn("80%", SYSTEM)
        self.assertIn("appear in one of that entry's cited passages", SYSTEM)

    def test_word_cap_overrun_gets_one_shortening_pass_then_full_rechecks(self):
        self.response_mode = "style_only"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary", "repair"], self.phases)
        self.assertEqual(1, len(pipeline.studies))
        card = pipeline.studies[0][1]["study"]
        self.assertLessEqual(len(str(card.get("bottom_line", "")).split()), 40)
        job = next(iter(self.journal.data["jobs"].values()))
        self.assertEqual(["card_style_validation_failed"], [row["check"] for row in job["repairs"]["primary"]])
        self.assertIn("bottom_line", self.repair_payloads[0]["violations"][0]["details"][0])
        self.assertEqual([], self.publish(pipeline) and [])

    def test_failed_shortening_is_staged_with_a_warning_after_one_repair(self):
        self.response_mode = "style_unrepairable"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary", "repair"], self.phases)
        (fingerprint,) = pipeline.staged
        job = next(iter(self.journal.data["jobs"].values()))
        self.assertEqual(["card_style_validation_failed"], [row["check"] for row in job["warnings"]])
        self.assertEqual({}, self.journal.data["holds"])
        packet = render_review(self.journal.data["review"][fingerprint])
        self.assertIn("Check these first", packet)
        self.assertIn("Publishing will fail", packet)
        self.assertEqual([], list((self.root / "editions").glob("*.json")))

    def test_only_one_repair_per_phase_then_the_item_is_held(self):
        self.response_mode = "style_and_excerpt"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary", "repair"], self.phases)
        self.assertFalse(pipeline.studies)

    def test_excerpt_and_number_failures_are_repaired_with_the_source_then_fully_rechecked(self):
        self.response_mode = "excerpt_and_number"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary", "repair"], self.phases)
        self.assertEqual(1, len(pipeline.studies))
        sent = self.repair_payloads[0]
        self.assertEqual(render_passages(ARTICLE), sent["article"])
        checks = {row["check"]: row for row in sent["violations"]}
        self.assertEqual({"unknown_passage_id", "unsupported_number"}, checks.keys())
        self.assertEqual(["evidence.results[0]"], checks["unknown_passage_id"]["fields"])
        self.assertEqual(f"P1 to P{len(passages(ARTICLE))}", checks["unknown_passage_id"]["details"]["valid_ids"])
        self.assertEqual(["15"], checks["unsupported_number"]["details"]["card.results[0]"])
        self.assertNotIn("15", json.dumps(pipeline.studies[0][1]["study"]["results"]))

    def test_methodological_failures_are_never_repaired(self):
        self.response_mode = "missing_domain"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary"], self.phases)
        job = next(iter(self.journal.data["jobs"].values()))
        self.assertIn("missing_bias_domains", [row["check"] for row in job["warnings"]])
        self.assertEqual({}, self.journal.data["holds"])

    def test_published_edition_does_not_overstate_what_was_checked(self):
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.publish(pipeline)
        edition = json.loads(next((self.root / "editions").glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn("one AI reading", edition["verdict"])
        self.assertIn("approved by a clinician", edition["verdict"])
        self.assertIn("Not independently verified", edition["verdict"])
        self.assertNotIn("two model", edition["verdict"].lower())

    def test_insufficient_source_and_empty_queue_do_not_call_model(self):
        self.pipeline().process()
        self.assertEqual({}, self.journal.data["holds"])
        pipeline = self.pipeline([RECORD], "insufficient")
        pipeline.process()
        self.assertIn("insufficient_source", self.reasons(self.journal.data["holds"]))
        self.assertEqual([], self.publish(pipeline))
        self.assertEqual([], self.phases)

    def test_a_checked_reading_is_staged_for_review_and_never_published_by_staging(self):
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary"], self.phases)
        (fingerprint,) = pipeline.staged
        entry = self.journal.data["review"][fingerprint]
        self.assertEqual("pending", entry["status"])
        self.assertEqual({}, {k: v for k, v in self.journal.data["holds"].items()})
        self.assertEqual([], list((self.root / "editions").glob("*.json")))
        packet = render_review(entry)
        self.assertIn("Risk of bias (RoB 2)", packet)
        self.assertIn(f"`{fingerprint[:12]}`", packet)
        self.assertIn(f"[{GOOD}] {QUOTE}", packet)  # the cited passage text, so the reviewer can check it
        self.assertNotIn("evidence_refs", json.dumps(entry["card"]))

    def test_only_approved_pending_fingerprints_can_be_promoted(self):
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        (fingerprint,) = pipeline.staged
        for bad in ([fingerprint[:12].replace(fingerprint[0], "z")], ["0" * 12]):
            with self.assertRaisesRegex(Unavailable, "promote_id_not_pending_or_ambiguous"):
                approved(self.journal, bad)
        papers, studies = approved(self.journal, [fingerprint[:12]])
        self.assertEqual((0, 1), (len(papers), len(studies)))
        self.journal.data["review"][fingerprint]["status"] = "published"
        with self.assertRaisesRegex(Unavailable, "promote_id_not_pending_or_ambiguous"):
            approved(self.journal, [fingerprint[:12]])

    def test_unsupported_number_is_flagged_for_review_and_not_published(self):
        self.response_mode = "invented"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        (fingerprint,) = pipeline.staged
        packet = render_review(self.journal.data["review"][fingerprint])
        self.assertIn("unsupported_number", packet)
        self.assertEqual([], list((self.root / "editions").glob("*.json")))

    def test_clean_reading_has_no_warnings(self):
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        (fingerprint,) = pipeline.staged
        self.assertEqual([], self.journal.data["review"][fingerprint]["warnings"])
        self.assertIn("No automated warnings", render_review(self.journal.data["review"][fingerprint]))

    def checked(self, mutate, source=ARTICLE):
        result = drop_null(reading("full_text", self.pipeline().schema))
        mutate(result)
        return result, source

    def run_checks(self, mutate):
        result, source = self.checked(mutate)
        appraisal_checks(result, source, "full_text")

    def test_evidence_cites_passage_ids_that_must_exist(self):
        for ref, reason in ((GOOD, None), (f"[{GOOD}]", None), ("P02", None), (" P2 ", None),
                            (BAD_ID, "unknown_passage_id"), ("P0", "unknown_passage_id"),
                            (QUOTE, "unknown_passage_id"), ("", "unknown_passage_id")):
            with self.subTest(ref=ref):
                result, source = self.checked(lambda r: r["evidence"].update(results=[ref]))
                if reason:
                    with self.assertRaisesRegex(Review, f"^{reason}$") as raised:
                        appraisal_checks(result, source, "full_text")
                    self.assertEqual(["evidence.results[0]"], raised.exception.fields)
                else:
                    appraisal_checks(result, source, "full_text")
        result, source = self.checked(lambda r: r["evidence"].update(results=[]))
        with self.assertRaisesRegex(Review, "^missing_source_excerpt$") as raised:
            appraisal_checks(result, source, "full_text")
        self.assertEqual(["evidence.results"], raised.exception.fields)

    def test_entry_numbers_must_appear_in_that_entrys_own_cited_passages(self):
        def entry(refs, others=(NUMBERS,)):
            def mutate(result):
                result["card"]["results"][0]["result"] = "Pain fell by 12 points"
                result["card"]["results"][0]["evidence_refs"] = refs
                result["evidence"]["numbers"] = list(others)
            return mutate
        # P4 is cited elsewhere in the reading, which does not support this entry.
        result, source = self.checked(entry([GOOD]))
        with self.assertRaisesRegex(Review, "^unsupported_number$") as raised:
            appraisal_checks(result, source, "full_text")
        self.assertEqual(["card.results[0]"], raised.exception.fields)
        self.assertEqual({"card.results[0]": ["12"]}, raised.exception.number_details)
        self.run_checks(entry([NUMBERS]))
        self.run_checks(entry([GOOD, NUMBERS]))

    def test_key_numbers_are_checked_against_their_own_refs(self):
        def mutate(refs):
            def inner(result):
                result["card"]["key_numbers"] = [{"value": "12", "label": "points", "evidence_refs": refs}]
            return inner
        self.run_checks(mutate([NUMBERS]))
        with self.assertRaisesRegex(Review, "^unsupported_number$") as raised:
            self.run_checks(mutate([GOOD]))
        self.assertEqual(["card.key_numbers[0]"], raised.exception.fields)

    def test_narrative_numbers_need_some_cited_passage(self):
        def mutate(cite):
            def inner(result):
                result["card"]["impact_line"] = "Pain fell by 12 points."
                result["evidence"]["numbers"] = [cite]
            return inner
        self.run_checks(mutate(NUMBERS))
        with self.assertRaisesRegex(Review, "^unsupported_number$") as raised:
            self.run_checks(mutate(GOOD))
        self.assertEqual(["card.impact_line"], raised.exception.fields)

    def test_result_entries_must_cite_at_least_one_passage(self):
        result, source = self.checked(lambda r: r["card"]["results"][0].update(evidence_refs=[]))
        with self.assertRaisesRegex(Review, "^unknown_passage_id$") as raised:
            appraisal_checks(result, source, "full_text")
        self.assertEqual(["card.results[0].evidence_refs"], raised.exception.fields)
        result, source = self.checked(lambda r: r["card"]["results"][0].update(evidence_refs=[BAD_ID]))
        with self.assertRaisesRegex(Review, "^unknown_passage_id$") as raised:
            appraisal_checks(result, source, "full_text")
        self.assertEqual(["card.results[0].evidence_refs[0]"], raised.exception.fields)

    def test_passages_are_deterministic_bounded_and_rendered_with_ids(self):
        long = "Sentence number one is here. " * 80
        article = "Heading\n" + long + "\n\nLast line."
        first = passages(article)
        self.assertEqual(first, passages(article))
        self.assertEqual([f"P{i}" for i in range(1, len(first) + 1)], [pid for pid, _ in first])
        self.assertTrue(all(0 < len(text) <= 700 for _, text in first))
        self.assertEqual("[P1] Heading", render_passages(article).splitlines()[0])
        self.assertEqual("P12", passage_id("[P012]"))
        self.assertIsNone(passage_id("The trial"))

    def test_evidence_refs_are_removed_before_the_card_is_validated_or_published(self):
        card = drop_null(reading("full_text", self.pipeline().schema))["card"]
        card["key_numbers"] = [{"value": "12", "label": "points", "evidence_refs": [NUMBERS]}]
        self.assertNotIn("evidence_refs", json.dumps(strip_refs(card)))
        self.assertIn("evidence_refs", json.dumps(card))
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(1, len(pipeline.studies))
        self.assertNotIn("evidence_refs", json.dumps(pipeline.studies[0][1]))
        self.publish(pipeline)

    def test_cited_ids_are_kept_before_the_repair_call_so_a_crash_stays_diagnosable(self):
        self.response_mode = "excerpt_and_number"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        job = next(iter(self.journal.data["jobs"].values()))
        sample = job["repair_refs"]["primary"]
        self.assertIn({"field": "evidence.results[0]", "ref": BAD_ID, "known": False}, sample)
        self.assertIn({"field": "card.results[0].evidence_refs[0]", "ref": GOOD, "known": True}, sample)
        self.assertTrue(all(len(row["ref"]) <= 80 for row in sample))
        long = {"evidence": {"design": ["P1 " + "x" * 500]}}
        self.assertEqual(80, len(ref_sample(long, ARTICLE)[0]["ref"]))

    def test_diagnostics_show_cited_passages_for_unknown_ids(self):
        rows = excerpt_diagnostics({"evidence": {"results": [GOOD, BAD_ID]}}, ARTICLE)
        self.assertEqual([True, False], [row["known"] for row in rows])
        self.assertEqual(QUOTE, rows[0]["passage"])
        self.assertEqual("", rows[1]["passage"])

    def test_benchmark_keeps_failed_reading_private_and_blocks_preview(self):
        self.response_mode = "primary"
        article = SOURCE_CONTEXT + "\n" + ARTICLE + "\nUnrelated private source text."
        literature = StubLiterature([RECORD])
        state = self.root / "private"
        state.mkdir()
        write_json(self.root / "scripts/research_config.json", CONFIG)
        feed_before = {path: path.read_bytes() for path in self.root.rglob("*.json")}
        stdout, stderr = StringIO(), StringIO()
        with patch("research.ROOT", self.root), patch("research.Literature", return_value=literature), \
                patch.object(literature, "source", return_value=("full_text", RECORD.url, article)), \
                patch("research.git") as git, \
                patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                                        "ATLAS_PUBLISH_ENABLED": "false"}), \
                patch.object(sys, "argv", ["research", "stage", "--state", str(state),
                                            "--benchmark-pmid", RECORD.pmid]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            # Ignore the state commit itself in this fixture. Every write must target private state.
            git.side_effect = lambda path, *args: state.as_posix() if args[0] == "rev-parse" else ""
            self.assertEqual(1, main())
            self.assertTrue(all(call.args[0] == state for call in git.call_args_list))
        self.assertEqual(["primary", "repair"], self.phases)
        journal = Journal(state / "research-state.json")
        run = journal.data["runs"]["123.1"]
        self.assertEqual("needs_review", run["status"])
        self.assertEqual(0, run["staged"])
        self.assertNotIn("preview", run)
        self.assertNotIn("published", run)
        self.assertEqual({"reading_passed_checks": False, "warnings": 0, "held_by": "unknown_passage_id"},
                         run["benchmark_checks"])
        job = next(iter(journal.data["jobs"].values()))
        failure = job["validation_failures"]["primary"]
        self.assertEqual("123.1", failure["run"])
        self.assertEqual(RECORD.pmid, failure["record"]["pmid"])
        self.assertEqual("held", failure["status"])
        self.assertEqual("unknown_passage_id", failure["reason"])
        self.assertEqual(["evidence.results[0]"], failure["fields"])
        excerpt = next(row for row in failure["excerpts"] if not row["known"])
        self.assertEqual({"field": "evidence.results[0]", "ref": BAD_ID, "known": False, "passage": ""}, excerpt)
        self.assertEqual([BAD_ID], failure["reading"]["evidence"]["results"])
        replay = revalidate(job, article)
        self.assertEqual("held", replay["primary"]["status"])
        self.assertEqual({"status": "source_hash_mismatch"}, revalidate(job, "other text"))
        public = stdout.getvalue() + stderr.getvalue()
        saved = journal.path.read_text(encoding="utf-8")
        for private in (QUOTE, SOURCE_CONTEXT):
            self.assertNotIn(private, public)
        for unwanted in ("fixture-only", "provider-envelope-only", "Unrelated private source text."):
            self.assertNotIn(unwanted, public)
            self.assertNotIn(unwanted, saved)
        self.assertEqual(feed_before, {path: path.read_bytes() for path in self.root.rglob("*.json")
                                       if path.parent != state})
        journal.expire_validation_failures(datetime.fromisoformat(failure["checked_at"]) + timedelta(days=30))
        self.assertNotIn("validation_failures", job)
        self.assertEqual("unknown_passage_id", job["hold"])

    def run_main(self, state, *args, **env):
        literature = StubLiterature([RECORD])
        write_json(self.root / "scripts/research_config.json", CONFIG)
        stdout = StringIO()
        with patch("research.ROOT", self.root), patch("research.Literature", return_value=literature), \
                patch("research.git") as git, \
                patch.dict(os.environ, {"GITHUB_RUN_ID": env.pop("run", "1"), "GITHUB_RUN_ATTEMPT": "1",
                                        "ATLAS_PUBLISH_ENABLED": "false", **env}), \
                patch.object(sys, "argv", ["research", *args, "--state", str(state)]), \
                redirect_stdout(stdout), redirect_stderr(StringIO()):
            git.side_effect = lambda path, *a: state.as_posix() if a[0] == "rev-parse" else ""
            return main(), stdout.getvalue()

    def test_stage_writes_a_review_file_and_promote_publishes_only_what_was_approved(self):
        state = self.root / "private"
        state.mkdir()
        code, out = self.run_main(state, "stage", "--benchmark-pmid", RECORD.pmid)
        self.assertEqual(0, code)
        self.assertIn("Staged for review: 1. Published: 0.", out)
        journal = Journal(state / "research-state.json")
        (fingerprint,) = journal.data["review"]
        packet = (state / "reviews" / f"{fingerprint[:12]}.md").read_text(encoding="utf-8")
        self.assertIn(RECORD.title, packet)
        self.assertEqual([], list((self.root / "editions").glob("*.json")))
        ready = dict(ATLAS_PUBLISH_ENABLED="true", GITHUB_EVENT_NAME="workflow_dispatch",
                     GITHUB_REF="refs/heads/main", run="2")
        for args, env in (
            (("promote", "--promote-ids", fingerprint[:12]), {**ready, "ATLAS_PUBLISH_ENABLED": "false"}),
            (("promote", "--promote-ids", fingerprint[:12]), {**ready, "GITHUB_EVENT_NAME": "schedule"}),
            (("promote", "--promote-ids", fingerprint[:12]), {**ready, "GITHUB_REF": "refs/heads/other"}),
            (("promote",), ready),
            (("promote", "--promote-ids", fingerprint[:8]), ready),
        ):
            with self.subTest(args=args, env=env), self.assertRaisesRegex(Unavailable, "publication_gate_closed"):
                self.run_main(state, *args, **env)
        self.assertEqual([], list((self.root / "editions").glob("*.json")))
        code, out = self.run_main(state, "promote", "--promote-ids", fingerprint[:12], **ready)
        self.assertEqual(0, code)
        self.assertIn("Published: 1.", out)
        edition = json.loads(next((self.root / "editions").glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn("approved by a clinician", edition["verdict"])
        entry = Journal(state / "research-state.json").data["review"][fingerprint]
        self.assertEqual("published", entry["status"])
        code, out = self.run_main(state, "promote", "--promote-ids", fingerprint[:12], **{**ready, "run": "3"})
        self.assertNotEqual(0, code)  # approving the same card twice is refused, not republished

    def test_malformed_model_text_is_held_and_retained_without_provider_envelope(self):
        self.response_mode = "invalid_json"
        pipeline = self.pipeline([RECORD])
        pipeline.process()
        self.assertEqual(["primary"], self.phases)
        self.assertFalse(pipeline.studies)
        job = next(iter(Journal(self.journal.path).data["jobs"].values()))
        failure = job["validation_failures"]["primary"]
        self.assertEqual("model_refusal_or_invalid_output", failure["reason"])
        self.assertEqual(["{private-invalid-json"], failure["output_text"])

    def test_rate_limit_resumes_after_an_uncertain_charge(self):
        self.response_mode = "failure"
        with self.assertRaisesRegex(Unavailable, "http_429"):
            self.pipeline([RECORD]).process()
        reloaded = Journal(self.journal.path)
        self.assertEqual("uncertain", reloaded.data["charges"][0]["status"])
        self.response_mode = ""
        pipeline = self.pipeline([RECORD], run_id="retry", journal=reloaded)
        # The failed candidate has moved outside the discovery window.
        with patch.object(pipeline.literature, "discover", return_value=[]):
            pipeline.process()
        self.assertTrue(self.publish(pipeline))
        self.assertEqual(["primary", "primary"], self.phases)

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
        self.assertEqual(other.doi, pipeline.studies[0][0].doi)
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

    def test_xml_keeps_inline_punctuation_and_separates_table_cells(self):
        xml = (f'<article><front><article-meta><article-id pub-id-type="pmid">12345</article-id>'
               f'</article-meta></front><body><sec><p>{ARTICLE}</p>'
               '<p>Pain scores were lower (<italic>p</italic>&lt;0.001).</p>'
               '<table-wrap><table><tr><th>Group</th><td>Exercise</td><td>5.0</td></tr>'
               '</table></table-wrap></sec></body></article>').encode()
        with patch("research_sources.get", return_value=xml):
            level, _, source = Literature().source(RECORD)
        self.assertIn("Group\tExercise\t5.0", source)
        result = drop_null(reading(level, self.pipeline().schema))
        pid = next(pid for pid, text in passages(source) if "Pain scores were lower (p<0.001)." in text)
        result["evidence"]["results"] = [pid]
        appraisal_checks(result, source, level)


if __name__ == "__main__":
    unittest.main()


class NumericValuesTest(unittest.TestCase):
    def test_sign_and_trailing_zero_match_source(self):
        from research_appraisal import numeric_values
        card = numeric_values("SR +12.0; ER +14.4; p=0.50")
        paper = numeric_values("improved by 12.0 (SR) and 14.4 (ER) points, p = 0.5")
        self.assertEqual(card - paper, set())

    def test_negative_still_distinct(self):
        from research_appraisal import numeric_values
        self.assertEqual(numeric_values("-3.2") - numeric_values("3.2"), {"-3.2"})
